"""
app.py - Cestas Atendente (Flask)

Atendimento automatizado via WhatsApp + Claude para clientes da Cestas Company
e Flower Store. Consome dados reais via Shopify Admin API direto (Fase 1) e,
em fases posteriores, via cestas-routes/cestas-company.

Estrutura:
  - GET  /health                                  healthcheck (Railway)
  - POST /webhook/zapi                            alias legacy → /webhook/zapi/cestascompany
  - POST /webhook/zapi/<shop_slug>                webhook Z-API por loja (cestascompany | flowerstore)
  - GET  /admin/api/conversations                 lista paginada de sessoes (Sprint 4.2)
  - GET  /admin/api/conversations/<id>            detalhe + mensagens (Sprint 4.2)
  - GET  /admin/api/handoffs                      fila de escalacoes pendentes (Sprint 4.2)
  - GET  /admin/api/metrics                       agregados pro dashboard (Sprint 4.2)
  - POST /admin/api/conversations/<id>/takeover   operador assume a conversa (Sprint 4.4)
  - POST /admin/api/conversations/<id>/release    operador devolve pra IA (Sprint 4.4)
  - POST /admin/api/conversations/<id>/send       operador envia mensagem via Z-API (Sprint 4.4)
  - GET  /admin/api/manual                        le manual da loja (Sprint 5.1)
  - PUT  /admin/api/manual                        atualiza manual da loja (Sprint 5.1)
  Todos /admin/api/* exigem Authorization: Bearer <INTERNAL_API_TOKEN>.

Multi-loja:
  Cada loja tem instancia Z-API propria com webhook apontando para
  /webhook/zapi/<shop_slug>. O slug resolve para o dominio Shopify oficial
  via SHOP_BY_SLUG, gravado em AtendenteSession.shop. Hoje so a Cestas
  Company esta ativa; o path flowerstore fica pronto pra ligar quando
  a 2a instancia Z-API for criada.

REGRAS DE OURO (herdadas dos projetos irmaos):
1. Nunca usar patches auto-correctivos — sempre arquivo completo
2. Verificar duplicatas antes de push
3. flask-cors resolve CORS — nao adicionar handlers manuais
4. Confirmar com deploy.py --check antes de deploy
"""
import os
import logging
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, request, jsonify
from flask_cors import CORS
from sqlalchemy import func, and_

from models import db, init_db, AtendenteSession, AtendenteMessage, AtendenteHandoff, AtendenteStoreManual
import zapi_adapter
import anthropic_adapter
import shopify_client


# ─────────────────────────────────────────────────────────────────────────────
# Configuracao do app
# ─────────────────────────────────────────────────────────────────────────────

LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
logger = logging.getLogger('cestas-atendente')

app = Flask(__name__)
CORS(app)  # libera para uso eventual via painel web em outro dominio

# ── Postgres (compartilhado com cestas-company) ──────────────────────────────
_DB_URL = os.environ.get('DATABASE_URL', '')
if _DB_URL.startswith('postgres://'):
    _DB_URL = _DB_URL.replace('postgres://', 'postgresql://', 1)

if _DB_URL:
    app.config['SQLALCHEMY_DATABASE_URI'] = _DB_URL
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_pre_ping': True,
        'connect_args': {'sslmode': 'require'},
    }
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    _DB_OK = init_db(app)
else:
    _DB_OK = False
    logger.warning('[boot] DATABASE_URL nao configurado — modo degradado')


# ─────────────────────────────────────────────────────────────────────────────
# Mapa de loja por slug do webhook
# ─────────────────────────────────────────────────────────────────────────────
# Cada loja Shopify roda uma instancia Z-API separada que aponta para um path
# proprio. Slug → dominio Shopify canonico gravado em AtendenteSession.shop.
SHOP_BY_SLUG = {
    'cestascompany': 'unicestas-8762.myshopify.com',
    # Configurar quando a 2a instancia Z-API for criada com WhatsApp da Flower:
    'flowerstore':   os.environ.get('FLOWERSTORE_SHOP_DOMAIN', '').strip()
                     or 'flowerstore.myshopify.com',
}
DEFAULT_SHOP_SLUG = 'cestascompany'


# ─────────────────────────────────────────────────────────────────────────────
# Helpers de sessao
# ─────────────────────────────────────────────────────────────────────────────

def _get_or_create_session(phone, shop, channel='whatsapp', name=''):
    """Encontra a sessao ativa para (loja, telefone, canal) ou cria uma nova.
    Sessoes em status 'handoff', 'human_active' ou 'closed' nao sao reutilizadas
    — nova sessao eh criada (mas preservamos o historico antigo no DB).

    O filtro por `shop` evita vazamento entre lojas quando o mesmo cliente
    conversa em mais de uma loja com o mesmo numero.
    """
    sess = (
        AtendenteSession.query
        .filter_by(shop=shop, phone=phone, channel=channel, status='active')
        .order_by(AtendenteSession.created_at.desc())
        .first()
    )
    if sess:
        return sess

    sess = AtendenteSession(
        shop=shop,
        channel=channel,
        phone=phone,
        status='active',
        customer_name=name or None,
        meta={'created_via': 'zapi_webhook'},
    )
    db.session.add(sess)
    db.session.commit()
    logger.info(f'[session] nova sessao criada id={sess.id} shop={shop} '
                f'channel={channel} phone={phone}')
    return sess


# ─────────────────────────────────────────────────────────────────────────────
# Rotas
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/health', methods=['GET'])
def health():
    """Healthcheck para Railway. Reporta status das dependencias externas."""
    return jsonify({
        'status': 'ok',
        'db': _DB_OK,
        'anthropic_configured': anthropic_adapter.is_configured(),
        'zapi_configured': zapi_adapter.is_configured(),
        'shopify_configured': shopify_client.is_configured(),
    }), 200


@app.route('/', methods=['GET'])
def root():
    return jsonify({
        'service': 'cestas-atendente',
        'version': '0.1.0',
        'docs': 'see ARCHITECTURE.md',
    }), 200


@app.route('/debug/shopify-sample', methods=['GET'])
def debug_shopify_sample():
    """Lista os ultimos N pedidos da Shopify com o campo `name` exposto.
    Util pra confirmar o formato real dos nomes (ex: '#CC3752' vs 'CC3752').
    Protegido por INTERNAL_API_TOKEN — defina essa env var no Railway pra
    habilitar o endpoint. Acesso: ?token=X ou header X-Internal-Token: X.
    """
    expected = os.environ.get('INTERNAL_API_TOKEN', '').strip()
    if not expected:
        return jsonify({
            'error': 'INTERNAL_API_TOKEN nao configurado no servidor — '
                     'defina essa env var no Railway para usar o debug.'
        }), 503

    provided = (
        request.args.get('token', '').strip()
        or request.headers.get('X-Internal-Token', '').strip()
    )
    if provided != expected:
        return jsonify({'error': 'unauthorized'}), 401

    limit = request.args.get('limit', default=5, type=int)
    return jsonify(shopify_client.list_recent_orders_sample(limit=limit)), 200


def _handle_zapi_webhook(shop_slug):
    """Logica do webhook compartilhada entre rota legacy e rota por loja.
    Resolve o dominio Shopify a partir do slug e roda IA / handoff.
    """
    shop_domain = SHOP_BY_SLUG.get(shop_slug)
    if not shop_domain:
        logger.warning(f'[webhook] shop_slug desconhecido: {shop_slug}')
        return jsonify({'ok': False, 'error': 'unknown shop slug'}), 404

    payload = request.get_json(silent=True) or {}
    parsed = zapi_adapter.parse_webhook(payload)

    if not parsed:
        # Evento ignorado (status, fromMe, grupo, midia sem texto, etc)
        return jsonify({'ok': True, 'ignored': True}), 200

    if not _DB_OK:
        logger.error('[webhook] DB nao disponivel — abortando')
        return jsonify({'ok': False, 'error': 'db unavailable'}), 503

    phone = parsed['phone']
    text = parsed['text']
    name = parsed.get('name', '')
    msg_id = parsed.get('message_id', '')

    logger.info(f'[webhook] shop={shop_slug} phone={phone} '
                f'text={text[:80]!r} msg_id={msg_id}')

    try:
        session = _get_or_create_session(phone, shop_domain, name=name)

        # Atualiza last_message_at e nome do cliente se vier
        from datetime import datetime
        session.last_message_at = datetime.utcnow()
        if name and not session.customer_name:
            session.customer_name = name
        db.session.commit()

        # Sessao em handoff/human_active: nao responder automaticamente, apenas registrar
        if session.status in ('handoff', 'human_active'):
            from models import AtendenteMessage
            db.session.add(AtendenteMessage(
                session_id=session.id,
                role='user',
                content=text,
                external_id=msg_id,
            ))
            db.session.commit()
            logger.info(f'[webhook] msg em sessao {session.id} '
                        f'({session.status}) — nao respondendo')
            return jsonify({'ok': True, 'handoff': True}), 200

        # Roda IA + tool loop
        reply = anthropic_adapter.respond(session, text, external_id=msg_id)

        if reply:
            ok, info = zapi_adapter.send_text(phone, reply)
            if not ok:
                logger.error(f'[webhook] falha envio Z-API: {info}')
            else:
                logger.info(f'[webhook] resposta enviada para {phone} '
                            f'({len(reply)} chars)')

        return jsonify({'ok': True, 'session_id': session.id}), 200

    except Exception as e:
        logger.exception(f'[webhook] erro inesperado: {e}')
        # Sempre 200 para Z-API nao retentar — erro fica no log
        return jsonify({'ok': False, 'error': str(e)[:200]}), 200


@app.route('/webhook/zapi', methods=['POST'])
def webhook_zapi_legacy():
    """Alias legacy: webhook unico antes do multi-loja (Sprint 4.1). A Z-API
    da Cestas Company segue apontando pra ca; quando atualizar o webhook
    URL no painel da Z-API, passa a usar /webhook/zapi/cestascompany.
    """
    return _handle_zapi_webhook(DEFAULT_SHOP_SLUG)


@app.route('/webhook/zapi/<shop_slug>', methods=['POST'])
def webhook_zapi_by_shop(shop_slug):
    """Webhook Z-API por loja. Cada instancia Z-API tem URL propria:
        /webhook/zapi/cestascompany   (instancia da Cestas Company)
        /webhook/zapi/flowerstore     (instancia da Flower Store — quando ligar)
    """
    return _handle_zapi_webhook(shop_slug)


# ─────────────────────────────────────────────────────────────────────────────
# API admin — consumida pelo painel do cestasapp via proxy server-side
# ─────────────────────────────────────────────────────────────────────────────

def _require_internal_token(fn):
    """Decorator: exige Authorization: Bearer <INTERNAL_API_TOKEN>.

    Usado pra todos endpoints /admin/api/*. O painel do cestasapp chama via
    proxy server-side (nao expoe o token pro browser).
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        expected = os.environ.get('INTERNAL_API_TOKEN', '').strip()
        if not expected:
            return jsonify({
                'error': 'INTERNAL_API_TOKEN nao configurado no servidor'
            }), 503

        auth = request.headers.get('Authorization', '')
        provided = ''
        if auth.startswith('Bearer '):
            provided = auth[len('Bearer '):].strip()

        if provided != expected:
            return jsonify({'error': 'unauthorized'}), 401

        return fn(*args, **kwargs)
    return wrapper


def _iso(dt):
    """Serializa datetime UTC em ISO 8601 com 'Z'. None -> None."""
    if dt is None:
        return None
    return dt.replace(microsecond=0).isoformat() + 'Z'


def _serialize_session_brief(s):
    """Versao curta de uma sessao — usada na listagem (sem mensagens)."""
    return {
        'id': s.id,
        'shop': s.shop,
        'channel': s.channel,
        'phone': s.phone,
        'status': s.status,
        'customer_id': s.customer_id,
        'customer_name': s.customer_name,
        'turn_count': s.turn_count,
        'tokens_input_total': s.tokens_input_total,
        'tokens_output_total': s.tokens_output_total,
        'cache_read_total': s.cache_read_total,
        'cache_write_total': s.cache_write_total,
        'created_at': _iso(s.created_at),
        'last_message_at': _iso(s.last_message_at),
        'meta': s.meta or {},
    }


def _serialize_message(m):
    """Mensagem com todos os blocos necessarios pro front renderizar.
    Inclui tool_calls (JSONB) pra mostrar o que a IA consultou."""
    return {
        'id': m.id,
        'session_id': m.session_id,
        'role': m.role,
        'content': m.content,
        'tool_calls': m.tool_calls,
        'model': m.model,
        'tokens_input': m.tokens_input,
        'tokens_output': m.tokens_output,
        'cache_read': m.cache_read,
        'cache_write': m.cache_write,
        'external_id': m.external_id,
        'created_at': _iso(m.created_at),
    }


def _serialize_handoff(h):
    return {
        'id': h.id,
        'session_id': h.session_id,
        'reason': h.reason,
        'summary': h.summary,
        'status': h.status,
        'assigned_to': h.assigned_to,
        'created_at': _iso(h.created_at),
        'resolved_at': _iso(h.resolved_at),
    }


@app.route('/admin/api/conversations', methods=['GET'])
@_require_internal_token
def admin_list_conversations():
    """Lista paginada de sessoes pro painel.

    Query params:
        shop     filtro por dominio Shopify (recomendado — uma loja por vez)
        status   filtro por status (active|handoff|human_active|closed|expired)
        q        busca textual em phone OU customer_name (ILIKE)
        limit    default 50, max 200
        offset   default 0
        order    'last_message' (default) ou 'created'

    Resposta:
        { "total": N, "limit": ..., "offset": ..., "items": [<session_brief>...] }
    """
    if not _DB_OK:
        return jsonify({'error': 'db unavailable'}), 503

    shop = (request.args.get('shop') or '').strip()
    status = (request.args.get('status') or '').strip()
    q = (request.args.get('q') or '').strip()

    try:
        limit = int(request.args.get('limit', 50))
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 200))

    try:
        offset = int(request.args.get('offset', 0))
    except (TypeError, ValueError):
        offset = 0
    offset = max(0, offset)

    order = (request.args.get('order') or 'last_message').strip()

    query = AtendenteSession.query
    if shop:
        query = query.filter(AtendenteSession.shop == shop)
    if status:
        query = query.filter(AtendenteSession.status == status)
    if q:
        like = f'%{q}%'
        query = query.filter(
            db.or_(
                AtendenteSession.phone.ilike(like),
                AtendenteSession.customer_name.ilike(like),
            )
        )

    total = query.count()

    if order == 'created':
        query = query.order_by(AtendenteSession.created_at.desc())
    else:
        query = query.order_by(AtendenteSession.last_message_at.desc())

    rows = query.limit(limit).offset(offset).all()

    return jsonify({
        'total': total,
        'limit': limit,
        'offset': offset,
        'items': [_serialize_session_brief(s) for s in rows],
    }), 200


@app.route('/admin/api/conversations/<int:session_id>', methods=['GET'])
@_require_internal_token
def admin_get_conversation(session_id):
    """Detalhe completo de uma sessao: dados + lista de mensagens
    (ordenada cronologicamente) + handoffs vinculados.

    Query params:
        msg_limit   limite de mensagens (default 500). Mensagens mais antigas
                    do que esse limite ficam de fora — front pode pedir mais
                    via msg_before_id quando precisar.
    """
    if not _DB_OK:
        return jsonify({'error': 'db unavailable'}), 503

    sess = AtendenteSession.query.get(session_id)
    if not sess:
        return jsonify({'error': 'not_found'}), 404

    try:
        msg_limit = int(request.args.get('msg_limit', 500))
    except (TypeError, ValueError):
        msg_limit = 500
    msg_limit = max(1, min(msg_limit, 2000))

    msgs = (
        AtendenteMessage.query
        .filter_by(session_id=sess.id)
        .order_by(AtendenteMessage.created_at.asc(), AtendenteMessage.id.asc())
        .limit(msg_limit)
        .all()
    )

    handoffs = (
        AtendenteHandoff.query
        .filter_by(session_id=sess.id)
        .order_by(AtendenteHandoff.created_at.asc())
        .all()
    )

    return jsonify({
        'session': _serialize_session_brief(sess),
        'messages': [_serialize_message(m) for m in msgs],
        'handoffs': [_serialize_handoff(h) for h in handoffs],
        'message_count_returned': len(msgs),
    }), 200


@app.route('/admin/api/handoffs', methods=['GET'])
@_require_internal_token
def admin_list_handoffs():
    """Fila de escalacoes. Default mostra pendentes (`status=pending`).

    Query params:
        shop      filtra pela loja da sessao vinculada
        status    pending (default) | taken | resolved
        limit     default 100, max 500
    """
    if not _DB_OK:
        return jsonify({'error': 'db unavailable'}), 503

    shop = (request.args.get('shop') or '').strip()
    status = (request.args.get('status') or 'pending').strip()

    try:
        limit = int(request.args.get('limit', 100))
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 500))

    query = (
        db.session.query(AtendenteHandoff, AtendenteSession)
        .join(AtendenteSession, AtendenteHandoff.session_id == AtendenteSession.id)
        .filter(AtendenteHandoff.status == status)
    )
    if shop:
        query = query.filter(AtendenteSession.shop == shop)

    query = query.order_by(AtendenteHandoff.created_at.desc()).limit(limit)

    items = []
    for h, s in query.all():
        item = _serialize_handoff(h)
        item['session'] = _serialize_session_brief(s)
        items.append(item)

    return jsonify({'items': items, 'count': len(items)}), 200


@app.route('/admin/api/metrics', methods=['GET'])
@_require_internal_token
def admin_metrics():
    """Agregados pro dashboard do painel.

    Query params:
        shop   filtra pela loja (recomendado)

    Resposta:
        {
          "now": "2026-05-16T...",
          "shop": "unicestas-...",
          "conversations": { "today": N, "last_7d": N, "last_30d": N,
                             "by_status": { "active": N, "handoff": N, ... } },
          "handoffs": { "pending": N, "taken": N, "resolved_30d": N },
          "tokens_7d": { "input": N, "output": N, "cache_read": N, "cache_write": N }
        }
    """
    if not _DB_OK:
        return jsonify({'error': 'db unavailable'}), 503

    shop = (request.args.get('shop') or '').strip()
    now = datetime.utcnow()
    start_today = datetime(now.year, now.month, now.day)
    start_7d = now - timedelta(days=7)
    start_30d = now - timedelta(days=30)

    sess_q = AtendenteSession.query
    if shop:
        sess_q = sess_q.filter(AtendenteSession.shop == shop)

    today = sess_q.filter(AtendenteSession.created_at >= start_today).count()
    last_7d = sess_q.filter(AtendenteSession.created_at >= start_7d).count()
    last_30d = sess_q.filter(AtendenteSession.created_at >= start_30d).count()

    by_status_rows = (
        sess_q.with_entities(AtendenteSession.status, func.count(AtendenteSession.id))
              .group_by(AtendenteSession.status)
              .all()
    )
    by_status = {row[0]: row[1] for row in by_status_rows}

    ho_q = (
        db.session.query(AtendenteHandoff)
        .join(AtendenteSession, AtendenteHandoff.session_id == AtendenteSession.id)
    )
    if shop:
        ho_q = ho_q.filter(AtendenteSession.shop == shop)

    ho_pending = ho_q.filter(AtendenteHandoff.status == 'pending').count()
    ho_taken = ho_q.filter(AtendenteHandoff.status == 'taken').count()
    ho_resolved_30d = ho_q.filter(
        and_(
            AtendenteHandoff.status == 'resolved',
            AtendenteHandoff.resolved_at >= start_30d,
        )
    ).count()

    tok_q = (
        db.session.query(
            func.coalesce(func.sum(AtendenteMessage.tokens_input), 0),
            func.coalesce(func.sum(AtendenteMessage.tokens_output), 0),
            func.coalesce(func.sum(AtendenteMessage.cache_read), 0),
            func.coalesce(func.sum(AtendenteMessage.cache_write), 0),
        )
        .join(AtendenteSession, AtendenteMessage.session_id == AtendenteSession.id)
        .filter(AtendenteMessage.created_at >= start_7d)
    )
    if shop:
        tok_q = tok_q.filter(AtendenteSession.shop == shop)
    tok_in, tok_out, tok_cr, tok_cw = tok_q.first()

    return jsonify({
        'now': _iso(now),
        'shop': shop or None,
        'conversations': {
            'today': today,
            'last_7d': last_7d,
            'last_30d': last_30d,
            'by_status': by_status,
        },
        'handoffs': {
            'pending': ho_pending,
            'taken': ho_taken,
            'resolved_30d': ho_resolved_30d,
        },
        'tokens_7d': {
            'input': int(tok_in or 0),
            'output': int(tok_out or 0),
            'cache_read': int(tok_cr or 0),
            'cache_write': int(tok_cw or 0),
        },
    }), 200


@app.route('/admin/api/conversations/<int:session_id>/takeover', methods=['POST'])
@_require_internal_token
def admin_takeover(session_id):
    """Operador assume a conversa: status -> human_active, IA fica muda.
    Resolve os handoffs pendentes vinculados.

    Body opcional (JSON):
        { "operator": "nome@email" }   — registrado em assigned_to dos handoffs
    """
    if not _DB_OK:
        return jsonify({'error': 'db unavailable'}), 503

    sess = AtendenteSession.query.get(session_id)
    if not sess:
        return jsonify({'error': 'not_found'}), 404

    if sess.status == 'human_active':
        return jsonify({'ok': True, 'already': True,
                        'session': _serialize_session_brief(sess)}), 200

    body = request.get_json(silent=True) or {}
    operator = (body.get('operator') or '').strip()[:128] or None

    sess.status = 'human_active'

    # Marca handoffs pending desta sessao como 'taken'
    now = datetime.utcnow()
    pendings = AtendenteHandoff.query.filter_by(
        session_id=sess.id, status='pending'
    ).all()
    for h in pendings:
        h.status = 'taken'
        if operator:
            h.assigned_to = operator

    db.session.commit()
    logger.info(f'[admin] takeover sess={sess.id} operator={operator!r} '
                f'handoffs_atualizados={len(pendings)}')

    return jsonify({
        'ok': True,
        'session': _serialize_session_brief(sess),
        'handoffs_taken': len(pendings),
    }), 200


@app.route('/admin/api/conversations/<int:session_id>/release', methods=['POST'])
@_require_internal_token
def admin_release(session_id):
    """Operador devolve a conversa pra IA: status human_active -> active.

    Body opcional (JSON):
        { "resolve_handoffs": true }   — marca handoffs taken como resolved
    """
    if not _DB_OK:
        return jsonify({'error': 'db unavailable'}), 503

    sess = AtendenteSession.query.get(session_id)
    if not sess:
        return jsonify({'error': 'not_found'}), 404

    body = request.get_json(silent=True) or {}
    resolve = bool(body.get('resolve_handoffs', True))

    sess.status = 'active'

    handoffs_resolved = 0
    if resolve:
        now = datetime.utcnow()
        rows = AtendenteHandoff.query.filter_by(
            session_id=sess.id, status='taken'
        ).all()
        for h in rows:
            h.status = 'resolved'
            h.resolved_at = now
            handoffs_resolved += 1

    db.session.commit()
    logger.info(f'[admin] release sess={sess.id} '
                f'handoffs_resolved={handoffs_resolved}')

    return jsonify({
        'ok': True,
        'session': _serialize_session_brief(sess),
        'handoffs_resolved': handoffs_resolved,
    }), 200


@app.route('/admin/api/conversations/<int:session_id>/send', methods=['POST'])
@_require_internal_token
def admin_send(session_id):
    """Operador envia mensagem manual pelo Z-API e grava no historico.

    Exige que a sessao esteja em status 'human_active' — protege contra envio
    enquanto a IA esta no controle (evita duas vozes simultaneas).

    Body (JSON):
        { "text": "mensagem...", "operator": "nome@email" }
    """
    if not _DB_OK:
        return jsonify({'error': 'db unavailable'}), 503

    sess = AtendenteSession.query.get(session_id)
    if not sess:
        return jsonify({'error': 'not_found'}), 404

    if sess.status != 'human_active':
        return jsonify({
            'error': 'invalid_state',
            'message': 'Assuma a conversa antes de enviar mensagens.',
            'current_status': sess.status,
        }), 409

    body = request.get_json(silent=True) or {}
    text = (body.get('text') or '').strip()
    if not text:
        return jsonify({'error': 'empty_text'}), 400
    if len(text) > 4000:
        return jsonify({'error': 'text_too_long', 'max': 4000}), 400

    operator = (body.get('operator') or '').strip()[:128] or None

    # 1. Envia via Z-API
    ok, info = zapi_adapter.send_text(sess.phone, text)
    if not ok:
        logger.error(f'[admin] send falhou sess={sess.id}: {info}')
        return jsonify({'error': 'zapi_send_failed', 'detail': str(info)[:300]}), 502

    # 2. Grava no historico como role='human' pra distinguir de assistant (IA)
    msg = AtendenteMessage(
        session_id=sess.id,
        role='human',
        content=text,
        external_id=(info or {}).get('messageId') or (info or {}).get('id'),
        # operador fica no tool_calls (campo JSONB livre) pra nao mexer no schema
        tool_calls={'operator': operator} if operator else None,
    )
    db.session.add(msg)
    sess.last_message_at = datetime.utcnow()
    sess.turn_count = (sess.turn_count or 0) + 1
    db.session.commit()

    logger.info(f'[admin] send sess={sess.id} operator={operator!r} '
                f'len={len(text)}')

    return jsonify({
        'ok': True,
        'message_id': msg.id,
        'external_id': msg.external_id,
    }), 200


@app.route('/admin/api/manual', methods=['GET'])
@_require_internal_token
def admin_get_manual():
    """Le o manual da loja. Query: ?shop=<dominio>. Cria registro vazio se ainda
    nao existir — facilita o frontend (sempre tem algo pra editar)."""
    if not _DB_OK:
        return jsonify({'error': 'db unavailable'}), 503

    shop = (request.args.get('shop') or '').strip()
    if not shop:
        return jsonify({'error': 'shop_required'}), 400

    row = AtendenteStoreManual.query.filter_by(shop=shop).first()
    if not row:
        return jsonify({
            'shop': shop,
            'manual_text': '',
            'updated_at': None,
            'updated_by': None,
            'exists': False,
        }), 200

    return jsonify({
        'shop': row.shop,
        'manual_text': row.manual_text or '',
        'updated_at': _iso(row.updated_at),
        'updated_by': row.updated_by,
        'exists': True,
    }), 200


@app.route('/admin/api/manual', methods=['PUT'])
@_require_internal_token
def admin_put_manual():
    """Atualiza o manual da loja. Upsert (cria se nao existir).
    Body JSON: { shop, manual_text, updated_by? }
    Limite: 20.000 caracteres pra evitar abuso e estourar contexto da IA.
    """
    if not _DB_OK:
        return jsonify({'error': 'db unavailable'}), 503

    body = request.get_json(silent=True) or {}
    shop = (body.get('shop') or '').strip()
    if not shop:
        return jsonify({'error': 'shop_required'}), 400

    manual_text = body.get('manual_text', '') or ''
    if len(manual_text) > 20000:
        return jsonify({'error': 'too_long', 'max': 20000,
                        'current': len(manual_text)}), 400

    updated_by = (body.get('updated_by') or '').strip()[:128] or None

    row = AtendenteStoreManual.query.filter_by(shop=shop).first()
    if row:
        row.manual_text = manual_text
        row.updated_by = updated_by
    else:
        row = AtendenteStoreManual(shop=shop, manual_text=manual_text,
                                    updated_by=updated_by)
        db.session.add(row)
    db.session.commit()

    # Invalida cache do adapter pra refletir mudanca rapidamente
    try:
        anthropic_adapter.invalidate_manual_cache(shop)
    except Exception as e:
        logger.warning(f'[manual] invalidate_cache falhou: {e}')

    logger.info(f'[manual] atualizado shop={shop} len={len(manual_text)} '
                f'updated_by={updated_by!r}')

    return jsonify({
        'shop': row.shop,
        'manual_text': row.manual_text,
        'updated_at': _iso(row.updated_at),
        'updated_by': row.updated_by,
    }), 200


# ─────────────────────────────────────────────────────────────────────────────
# Boot
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '5000'))
    logger.info(f'[boot] cestas-atendente iniciando em :{port}')
    logger.info(f'[boot] db={_DB_OK} '
                f'anthropic={anthropic_adapter.is_configured()} '
                f'zapi={zapi_adapter.is_configured()} '
                f'shopify={shopify_client.is_configured()}')
    app.run(host='0.0.0.0', port=port, debug=False)
