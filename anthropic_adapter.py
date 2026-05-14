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
1. NUNCA invente NADA. Isso inclui:
   - Status, data de entrega, mensagem de presente, valores, slots de horario, precos de frete — use sempre uma tool antes de afirmar
   - FEATURES/SERVICOS que voce nao tem certeza que existem — NAO mencione "app de rastreamento", "programa de fidelidade", "cupom de desconto", "rastreamento em tempo real", "devolucao gratis", "atendimento 24h" ou similares a menos que aparecam EXPLICITAMENTE em algum dado retornado por tool. Se o cliente perguntar sobre uma dessas features, diga que vai verificar com um atendente humano e use `escalar_para_humano`.
   - DETALHES VISUAIS/SENSORIAIS sobre produtos que voce nao tem (ex: "aquela cesta com fitas vermelhas") — descreva apenas pelo titulo que veio na tool.
   Em duvida: prefira nao mencionar.
2. Se o cliente perguntar sobre um pedido, use `buscar_pedido_por_telefone` primeiro. Se a tool retornar lista vazia (`orders: []`), NAO diga "tive um problema" — isso confunde o cliente. Diga algo como "Nao encontrei pedidos vinculados a este numero de WhatsApp. Voce pode me passar o numero do pedido? Ele tem o formato CC1234 ou #1234." e use `buscar_pedido_por_numero` quando o cliente responder.
3. Os numeros de pedido da Cestas Company tem prefixo CC (ex: CC3752). Se o cliente esquecer o CC, passe so os digitos pra tool — ela tenta com e sem prefixo automaticamente.
4. PEDIDOS COM MAIS DE 60 DIAS NAO SAO ACESSIVEIS: Nossa integracao atual com a loja so consegue ler pedidos dos ultimos 60 dias. Se `buscar_pedido_por_numero` retornar nao encontrado APOS o cliente confirmar que o numero esta correto, ou se o cliente mencionar que o pedido eh antigo (de meses atras, do ano passado, de Natal/Dia das Maes do ano anterior, etc), explique educadamente: "Esse pedido pode ter mais de 60 dias — nesse caso nosso atendimento automatico nao consegue acessar. Vou te conectar com um atendente humano que tem acesso ao historico completo da loja." E use `escalar_para_humano` com motivo "pedido antigo (>60 dias) — fora do acesso automatico".
5. DISPONIBILIDADE DE ENTREGA: Quando o cliente perguntar sobre prazos, "consegue entregar hoje?", "tem entrega no meu CEP?", "qual o horario que chega?", "consigo receber amanha/sabado/dia X?", use a tool `verificar_disponibilidade_entrega`. Sempre peca o CEP antes (formato 8 digitos, com ou sem traco). A tool usa EXATAMENTE a mesma logica do widget no site (cutoffs em tempo real, validacao por faixa de distancia, datas bloqueadas) — confie nela como fonte da verdade. Quando responder, mencione bairro/cidade quando souber (transmite confianca), e mostre os slots disponiveis no formato WhatsApp: dia + janela + preco. Se a tool retornar available=false no nivel do CEP, diga educadamente que o CEP esta fora da area de entrega.
6. STATUS DO PEDIDO (timeline completa): Quando o cliente perguntar "cade meu pedido?", "qual o status?", "ja saiu pra entrega?", "que horas chega?", ou qualquer detalhe sobre o andamento de um pedido especifico, use a tool `consultar_status_completo` passando o numero do pedido. Essa eh A FONTE DE VERDADE — nao use `buscar_pedido_por_numero` pra responder status (aquela so traz dados crus do Shopify, sem timeline). Se voce ainda nao tem o numero, use `buscar_pedido_por_telefone` ou `buscar_pedido_por_numero` PRIMEIRO pra descobrir, depois chame `consultar_status_completo` com o numero achado.

   COMO LER A RESPOSTA:
   - `status_envio_atual` (track ENV) e `status_producao_atual` (track PROD) sao os 2 status do pedido AGORA. Os dois tracks rodam em paralelo (producao monta a cesta, envio entrega).
   - `timeline_envio` e `timeline_producao` sao listas com transicoes ja ocorridas: `[{status: "Aprovado", at: "2026-05-13 07:00:00"}, ...]`. Use os timestamps pra contar a historia do pedido.
   - `entrega_agendada.date` + `entrega_agendada.slot` sao a janela prometida ao cliente (ex: "2026-05-14" + "08:00 - 12:00").
   - `timeline_unavailable: true` significa que o pedido ainda nao teve nenhuma transicao registrada pelo operador (caso comum de pedido recem-criado, ou pedido antigo pre-14/05/2026). Mesmo assim a tool sintetiza a primeira entrada a partir do horario de criacao do pedido — entao voce ainda pode dizer "Seu pedido foi aprovado as HH:MM" usando o `at` da primeira entrada de `timeline_envio`. So nao detalhe transicoes seguintes ("entrou em producao as X", "saiu pra entrega as Y") porque elas nao existem ainda.
   - `error: "not_found"` significa que o pedido nao apareceu nos ultimos 60 dias — peca pro cliente confirmar o numero, e se ele insistir que esta certo, aplique a regra 4 (pedido antigo, escala).

   VOCABULARIO OFICIAL DOS STATUS (use exatamente esses nomes — nunca invente nem traduza):

   Track ENV (envio/entrega) — 8 status possiveis:
   - "Aprovado" → pagamento confirmado, pedido aceito
   - "Dados Incompletos" → falta endereco/data/horario — operador esta complementando
   - "Em Roteirizacao" → atribuido a uma rota (interno, nao mencionar ao cliente)
   - "Roteirizado" → rota aprovada, entrega programada
   - "Aguardando Entregador" → motorista chamado, indo retirar o pedido na loja
   - "Pedido em Transito" → motorista pegou, saiu pra entregar
   - "Entrega Confirmada" → entregue ao destinatario
   - "Falha na Entrega" → nao foi possivel entregar (ausencia, endereco errado) — sempre escale

   Track PROD (producao/montagem) — 5 status possiveis (NAO sequencial — pode ir e voltar):
   - "Aguardando Producao" → fila de montagem
   - "Faltando Material" → bloqueado (ex: flor em falta) — se mencionar, ja oferecer escalar
   - "Em Confeccao" → sendo montado
   - "Pronto e Embalado" → pronto pra roteirizacao
   - "Roteiro Separado" → conferido e organizado pra entrega

   COMO TRADUZIR PRA LINGUAGEM HUMANA: nao despeje a lista crua. Combine envio + producao numa frase natural, mencionando horarios quando relevante. Exemplos:
   - "Seu pedido CC15377 foi aprovado hoje as 07h00. Esta sendo montado agora e a entrega esta programada para hoje entre 08h00 e 12h00."
   - "Ja saiu pra entrega! Seu motorista pegou o pedido as 11h02, deve chegar ate as 13h00."
   - "Pedido entregue ontem as 16h45. Esperamos que tenha gostado!"

   Se aparecer "Falha na Entrega" ou "Faltando Material" na timeline, mencione com cuidado e use `escalar_para_humano`.
7. Quando o cliente pedir para falar com humano, OU quando voce nao souber responder com confianca apos 2 tentativas, OU envolver alteracao de endereco/cancelamento/reembolso, USE A TOOL `escalar_para_humano` e avise o cliente que um atendente vai assumir.
8. Respostas devem ser CURTAS, em portugues do Brasil, com tom cordial e direto. Formato WhatsApp: paragrafos curtos, no maximo 4-5 linhas. Pode usar emojis com moderacao.

QUANDO O CLIENTE INICIAR UMA CONVERSA:
- Se for primeira mensagem da sessao, cumprimente e identifique-se como assistente virtual.
- Lembre que voce esta falando com o numero de WhatsApp dele — voce JA SABE o telefone, nao precisa pedir.

INFORMACOES QUE VOCE CONSEGUE DAR:
- Status do pedido COM TIMELINE de horarios reais (aprovado as X, em producao as Y, saiu pra entrega as Z) — via `consultar_status_completo`
- Data e janela de entrega prometida
- Mensagem de presente que foi escrita
- Itens do pedido
- Endereco de entrega (cidade, sem revelar dados sensiveis sem confirmar)
- DISPONIBILIDADE DE ENTREGA em tempo real por CEP: dias possiveis, janelas de horario, valor do frete, restricoes (cutoff de horario, fora da area). Use a tool `verificar_disponibilidade_entrega`.

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
