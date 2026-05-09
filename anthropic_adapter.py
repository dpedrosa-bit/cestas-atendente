"""
anthropic_adapter.py - Camada de IA do cestas-atendente

Responsabilidades:
1. Construir o prompt (system + history + user message) para uma sessao.
2. Rodar o loop de tool use do Claude (manual loop — controle fino sobre
   logging, escalacao e metricas, conforme guia oficial da SDK).
3. Aplicar prompt caching no system prompt para reduzir custo em ~90% das
   requisicoes em uma mesma conversa.
4. Persistir cada turno em atendente_messages com metricas.

Estrategia de modelos (controlavel via ENV):
- Default:  claude-haiku-4-5   (rapido, barato, suficiente para 70% das queries)
- Complexo: claude-sonnet-4-6   (raciocinio mais profundo + adaptive thinking)
- Escalacao: nao usamos modelo maior automaticamente; o agente decide chamar
  a tool `escalar_para_humano` quando nao tem confianca.

Prompt caching:
- system prompt fica no formato lista [{type:"text", text:..., cache_control:...}]
- tool definitions sao deterministicas (lista TOOL_DEFINITIONS importada)
- breakpoint no ultimo bloco do system → cacheia tools+system juntos
- TTL default de 5min cobre conversas em andamento; em horas de pico vale
  considerar TTL=1h, mas custo de write 2x nao compensa para o nosso volume.
"""
import os
import json
import time
import logging

import anthropic

from models import db, AtendenteMessage
from tools import TOOL_DEFINITIONS, run_tool

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Configuracao
# ─────────────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
MODEL_DEFAULT = os.environ.get('ANTHROPIC_MODEL_DEFAULT', 'claude-haiku-4-5')
MODEL_COMPLEX = os.environ.get('ANTHROPIC_MODEL_COMPLEX', 'claude-sonnet-4-6')
MAX_TURNS_PER_SESSION = int(os.environ.get('MAX_TURNS_PER_SESSION', '20'))
MAX_TOOL_LOOPS = int(os.environ.get('MAX_TOOL_LOOPS', '6'))
MAX_TOKENS_OUTPUT = int(os.environ.get('MAX_TOKENS_OUTPUT', '1024'))


def is_configured():
    return bool(ANTHROPIC_API_KEY)


# Cliente lazy (instanciado na primeira chamada)
_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


# ─────────────────────────────────────────────────────────────────────────────
# System prompt — placeholder para Fase 1
# ─────────────────────────────────────────────────────────────────────────────
# Quando o usuario fornecer tom de voz e regras especificas, substituimos esse
# texto. Por ora ele eh suficiente para o MVP responder consultas reais.
#
# REGRAS DE OURO embutidas no prompt:
# 1. Nunca inventar dados — sempre usar tool antes de afirmar status
# 2. Escalar para humano em caso de duvida ou pedido sensivel
# 3. Resposta curta e direta (formato WhatsApp)

SYSTEM_PROMPT = """Voce e o atendente virtual da Cestas Company, uma loja de cestas de presente. Atende clientes pelo WhatsApp.

REGRAS ABSOLUTAS:
1. NUNCA invente status, data de entrega, mensagem de presente, ou qualquer detalhe de pedido. SEMPRE use uma tool para consultar antes de afirmar.
2. Se o cliente perguntar sobre um pedido, use `buscar_pedido_por_telefone` ou `buscar_pedido_por_numero` antes de responder. Se nao encontrar nada, peca o numero do pedido.
3. Quando o cliente pedir para falar com humano, OU quando voce nao souber responder com confianca apos 2 tentativas, OU envolver alteracao de endereco/cancelamento/reembolso, USE A TOOL `escalar_para_humano` e avise o cliente que um atendente vai assumir.
4. Respostas devem ser CURTAS, em portugues do Brasil, com tom cordial e direto. Formato WhatsApp: paragrafos curtos, no maximo 4-5 linhas. Pode usar emojis com moderacao.

QUANDO O CLIENTE INICIAR UMA CONVERSA:
- Se for primeira mensagem da sessao, cumprimente e identifique-se como assistente virtual.
- Lembre que voce esta falando com o numero de WhatsApp dele — voce JA SABE o telefone, nao precisa pedir.

INFORMACOES QUE VOCE CONSEGUE DAR:
- Status do pedido (aprovado, em producao, saiu para entrega, entregue)
- Data e janela de entrega prometida
- Mensagem de presente que foi escrita
- Itens do pedido
- Endereco de entrega (cidade, sem revelar dados sensiveis sem confirmar)

INFORMACOES QUE VOCE NAO DEVE TENTAR DAR (escalar para humano):
- Reembolsos, cancelamentos, trocas
- Alteracao de endereco apos despacho
- Reclamacoes sobre produto danificado
- Qualquer valor monetario alem do total ja registrado no pedido
- Negociacao de prazo ou frete

LIMITE: maximo 20 mensagens por sessao. Se chegar perto disso e ainda nao resolveu, escale.
"""


def _build_system_blocks():
    """System prompt como lista de blocks com cache_control no ultimo.
    Render order: tools -> system -> messages. O breakpoint aqui cacheia
    tools+system juntos (~1.2k tokens, dentro do minimo de 4096 do Haiku 4.5
    so se somar com tools — verificar usage.cache_creation no primeiro turno
    e ajustar se nao bater)."""
    return [
        {
            'type': 'text',
            'text': SYSTEM_PROMPT,
            'cache_control': {'type': 'ephemeral'},
        }
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Reconstrucao do historico a partir do Postgres
# ─────────────────────────────────────────────────────────────────────────────

def _load_history(session):
    """Reconstrói a lista `messages` no formato esperado pela API a partir
    das linhas em atendente_messages. Inclui tool_use/tool_result blocks
    para que o agente tenha contexto completo do que ja foi consultado."""
    rows = (
        AtendenteMessage.query
        .filter_by(session_id=session.id)
        .order_by(AtendenteMessage.created_at.asc(), AtendenteMessage.id.asc())
        .all()
    )

    # Percorre rows agrupando tool_results consecutivos numa unica user msg.
    # A API exige: cada tool_use de um assistant turn precisa de um
    # tool_result correspondente no proximo user turn (ou turns).
    messages = []
    i = 0
    while i < len(rows):
        r = rows[i]
        if r.role == 'user':
            messages.append({'role': 'user', 'content': r.content})
            i += 1
        elif r.role == 'assistant':
            blocks = []
            if r.content:
                blocks.append({'type': 'text', 'text': r.content})
            if r.tool_calls:
                for tu in r.tool_calls:
                    blocks.append({
                        'type': 'tool_use',
                        'id': tu.get('id'),
                        'name': tu.get('name'),
                        'input': tu.get('input') or {},
                    })
            if blocks:
                messages.append({'role': 'assistant', 'content': blocks})
            i += 1
        elif r.role == 'tool':
            # agrupa todos os tool consecutivos em um unico user turn
            tool_blocks = []
            while i < len(rows) and rows[i].role == 'tool':
                tcalls = rows[i].tool_calls or {}
                tool_use_id = tcalls.get('tool_use_id')
                if tool_use_id:
                    tool_blocks.append({
                        'type': 'tool_result',
                        'tool_use_id': tool_use_id,
                        'content': rows[i].content or '{}',
                    })
                i += 1
            if tool_blocks:
                messages.append({'role': 'user', 'content': tool_blocks})
        else:
            i += 1

    return messages


# ─────────────────────────────────────────────────────────────────────────────
# Persistencia de turnos
# ─────────────────────────────────────────────────────────────────────────────

def _persist_user_message(session, text, external_id=None):
    msg = AtendenteMessage(
        session_id=session.id,
        role='user',
        content=text,
        external_id=external_id,
    )
    db.session.add(msg)
    session.turn_count = (session.turn_count or 0) + 1
    db.session.commit()
    return msg


def _persist_assistant_message(session, response, model_used):
    """Salva a resposta do Claude — texto + tool_use blocks + metricas."""
    text_blocks = [b for b in response.content if b.type == 'text']
    tool_use_blocks = [b for b in response.content if b.type == 'tool_use']

    text = '\n'.join(b.text for b in text_blocks) if text_blocks else ''

    tool_calls_json = None
    if tool_use_blocks:
        tool_calls_json = [
            {'id': b.id, 'name': b.name, 'input': b.input}
            for b in tool_use_blocks
        ]

    usage = response.usage
    msg = AtendenteMessage(
        session_id=session.id,
        role='assistant',
        content=text,
        tool_calls=tool_calls_json,
        model=model_used,
        tokens_input=getattr(usage, 'input_tokens', None),
        tokens_output=getattr(usage, 'output_tokens', None),
        cache_read=getattr(usage, 'cache_read_input_tokens', None),
        cache_write=getattr(usage, 'cache_creation_input_tokens', None),
    )
    db.session.add(msg)

    # Atualiza totais agregados na sessao
    session.tokens_input_total = (session.tokens_input_total or 0) + (msg.tokens_input or 0)
    session.tokens_output_total = (session.tokens_output_total or 0) + (msg.tokens_output or 0)
    session.cache_read_total = (session.cache_read_total or 0) + (msg.cache_read or 0)
    session.cache_write_total = (session.cache_write_total or 0) + (msg.cache_write or 0)

    db.session.commit()
    return msg


def _persist_tool_result(session, tool_use_id, tool_name, result):
    """Salva o resultado de uma tool no historico."""
    msg = AtendenteMessage(
        session_id=session.id,
        role='tool',
        content=json.dumps(result, ensure_ascii=False, default=str)[:50000],
        tool_calls={'tool_use_id': tool_use_id, 'name': tool_name},
    )
    db.session.add(msg)
    db.session.commit()
    return msg


# ─────────────────────────────────────────────────────────────────────────────
# Tool use loop (manual)
# ─────────────────────────────────────────────────────────────────────────────

def _run_tool_loop(session, model, messages):
    """Executa o loop manual de tool use ate end_turn ou MAX_TOOL_LOOPS.
    Retorna o texto final que sera enviado ao cliente."""
    client = _get_client()
    tool_context = {
        'session': session,
        'phone': session.phone,
    }

    final_text_parts = []

    for loop_idx in range(MAX_TOOL_LOOPS):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS_OUTPUT,
                system=_build_system_blocks(),
                tools=TOOL_DEFINITIONS,
                messages=messages,
            )
        except anthropic.RateLimitError as e:
            logger.warning(f'[atendente] rate limit no Anthropic API: {e}')
            return 'Estou com muitas conversas no momento, pode mandar de novo em alguns segundos? 🙏'
        except anthropic.APIStatusError as e:
            logger.error(f'[atendente] erro Anthropic API ({e.status_code}): {e.message}')
            return 'Tive um problema ao processar sua mensagem. Vou chamar um atendente humano.'
        except Exception as e:
            logger.exception(f'[atendente] erro inesperado: {e}')
            return 'Tive um problema agora. Pode tentar de novo em instantes?'

        # Persiste a resposta do assistente (texto + tool_use blocks juntos)
        _persist_assistant_message(session, response, model)

        # Coleta texto e tool calls
        text_blocks = [b for b in response.content if b.type == 'text']
        tool_use_blocks = [b for b in response.content if b.type == 'tool_use']
        if text_blocks:
            final_text_parts = [b.text for b in text_blocks]

        if response.stop_reason == 'end_turn' or not tool_use_blocks:
            break

        # Executa cada tool e empilha o assistant turn + user turn no contexto
        # (para que a proxima iteracao do loop tenha o contexto completo).
        # SDK Anthropic aceita os blocks Pydantic direto — round-trip nativo.
        messages.append({
            'role': 'assistant',
            'content': response.content,
        })

        tool_result_blocks = []
        for tu in tool_use_blocks:
            logger.info(f'[atendente] tool_call={tu.name} input={tu.input}')
            t0 = time.time()
            result = run_tool(tu.name, tu.input or {}, tool_context)
            logger.info(f'[atendente] tool_done={tu.name} {(time.time()-t0)*1000:.0f}ms')

            _persist_tool_result(session, tu.id, tu.name, result)

            tool_result_blocks.append({
                'type': 'tool_result',
                'tool_use_id': tu.id,
                'content': json.dumps(result, ensure_ascii=False, default=str)[:50000],
            })

            # Side-effects de algumas tools
            if tu.name == 'escalar_para_humano' and result.get('escalated'):
                # session.status ja foi atualizado dentro da tool
                pass

        messages.append({'role': 'user', 'content': tool_result_blocks})

    if not final_text_parts:
        # Loop estourou sem texto — escalar
        return ('Vou chamar um atendente humano para te ajudar — '
                'ele assume daqui em alguns minutos.')

    return '\n'.join(final_text_parts).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point publico
# ─────────────────────────────────────────────────────────────────────────────

def respond(session, user_message, external_id=None, force_complex=False):
    """Recebe a mensagem do cliente e retorna o texto a enviar de volta.

    Args:
        session: AtendenteSession ativa
        user_message: texto cru recebido do WhatsApp
        external_id: id da mensagem na Z-API (para idempotencia)
        force_complex: forca uso do modelo Sonnet (ex.: depois de N falhas)

    Returns:
        str: texto a enviar ao cliente (vazio se nada deve ser respondido)
    """
    if not is_configured():
        logger.error('[atendente] ANTHROPIC_API_KEY nao configurado')
        return 'Nosso atendimento automatico esta em manutencao. Vou avisar a equipe.'

    if session.turn_count and session.turn_count >= MAX_TURNS_PER_SESSION:
        logger.warning(f'[atendente] sessao {session.id} excedeu MAX_TURNS — escalando')
        # Forca escalacao silenciosa
        from models import AtendenteHandoff
        h = AtendenteHandoff(
            session_id=session.id,
            reason='Limite de turnos por sessao atingido',
            summary='Conversa longa sem resolucao — encaminhada automaticamente.',
            status='pending',
        )
        session.status = 'handoff'
        db.session.add(h)
        db.session.commit()
        return ('Para nao perdermos qualidade, vou passar nossa conversa para '
                'um atendente humano. Em breve alguem da equipe assume aqui. 🙏')

    # 1. Persiste a mensagem do usuario
    _persist_user_message(session, user_message, external_id=external_id)

    # 2. Reconstroi historico completo (incluindo a msg que acabamos de salvar)
    messages = _load_history(session)

    # 3. Escolhe modelo
    model = MODEL_COMPLEX if force_complex else MODEL_DEFAULT

    # 4. Roda o loop
    final_text = _run_tool_loop(session, model, messages)
    return final_text
