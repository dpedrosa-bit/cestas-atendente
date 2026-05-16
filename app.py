"""
app.py - Cestas Atendente (Flask)

Atendimento automatizado via WhatsApp + Claude para clientes da Cestas Company
e Flower Store. Consome dados reais via Shopify Admin API direto (Fase 1) e,
em fases posteriores, via cestas-routes/cestas-company.

Estrutura:
  - GET  /health                          healthcheck (Railway)
  - POST /webhook/zapi                    alias legacy → /webhook/zapi/cestascompany
  - POST /webhook/zapi/<shop_slug>        webhook Z-API por loja (cestascompany | flowerstore)
  - GET  /admin/sessions                  (Sprint 4.2) listagem de sessoes
  - GET  /admin/handoff                   (Sprint 4.2) fila de escalacao

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

from flask import Flask, request, jsonify
from flask_cors import CORS

from models import db, init_db, AtendenteSession
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
