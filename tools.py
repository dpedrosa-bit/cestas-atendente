"""
tools.py - Definições de ferramentas (tool use) e suas implementações

Padrão Anthropic tool use:
  TOOL_DEFINITIONS é a lista exposta para o Claude no parâmetro `tools=`.
  TOOL_IMPL é o dict {nome_tool: função(input_dict) -> resultado_dict}.

REGRA DE OURO: Tools NUNCA podem inventar dados. Sempre consultam fonte real
(Shopify, cestas-routes, cestas-company) e retornam erro estruturado se falhar.
O agente usa esse erro para responder honestamente ("não encontrei seu pedido,
posso transferir para um atendente humano?").

Estado do MVP (Fase 1): apenas leitura.
- buscar_pedido_por_telefone     [implementado]
- buscar_pedido_por_numero       [implementado]
- escalar_para_humano            [implementado — apenas marca a sessão]

Próximas fases:
- status_producao(order_id)        -> via cestas-routes
- status_entrega(order_id)         -> via cestas-routes (rota, motorista, ETA)
- slots_disponiveis(data, cep)     -> via cestas-company
"""
import shopify_client


# ─────────────────────────────────────────────────────────────────────────────
# Definições expostas para o Claude
# ─────────────────────────────────────────────────────────────────────────────

TOOL_DEFINITIONS = [
    {
        'name': 'buscar_pedido_por_telefone',
        'description': (
            'Busca os últimos pedidos do cliente na loja usando o número de telefone do WhatsApp. '
            'Use SEMPRE essa ferramenta quando o cliente perguntar sobre o status, data de entrega, '
            'mensagem de presente, ou qualquer detalhe de um pedido sem informar o número específico. '
            'Retorna até 5 pedidos mais recentes ordenados do mais novo para o mais antigo. '
            'Se não encontrar, retorna lista vazia — neste caso, peça ao cliente o número do pedido.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'phone': {
                    'type': 'string',
                    'description': (
                        'Número de telefone em qualquer formato (com ou sem +55, com ou sem 9). '
                        'A ferramenta normaliza e tenta múltiplas variantes automaticamente.'
                    ),
                },
            },
            'required': ['phone'],
        },
    },
    {
        'name': 'buscar_pedido_por_numero',
        'description': (
            'Busca um pedido específico pelo número informado pelo cliente. '
            'Aceita formatos "CC12345", "#12345" ou apenas "12345". '
            'Use quando o cliente fornecer o número do pedido explicitamente.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'order_number': {
                    'type': 'string',
                    'description': 'Número do pedido como o cliente informou.',
                },
            },
            'required': ['order_number'],
        },
    },
    {
        'name': 'escalar_para_humano',
        'description': (
            'Sinaliza que esta conversa precisa de atenção humana. Use quando: '
            '(1) o cliente pedir explicitamente para falar com um atendente humano, '
            '(2) houver reclamação grave ou ameaça de devolução/contestação, '
            '(3) você não souber responder com confiança após 2 tentativas, '
            '(4) envolver alteração de dados sensíveis (endereço, cancelamento, reembolso). '
            'Após chamar essa ferramenta, AVISE o cliente que um atendente humano vai assumir '
            'em breve e pare de tentar resolver sozinho.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'motivo': {
                    'type': 'string',
                    'description': 'Motivo curto (1 frase) do porquê está escalando.',
                },
                'resumo': {
                    'type': 'string',
                    'description': (
                        'Resumo do que o cliente quer e do que já foi conversado, em 2-3 frases, '
                        'para o atendente humano contextualizar rapidamente.'
                    ),
                },
            },
            'required': ['motivo', 'resumo'],
        },
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# Implementações
# ─────────────────────────────────────────────────────────────────────────────

def _tool_buscar_pedido_por_telefone(input_data, context):
    """context = { 'session': AtendenteSession, 'phone': '5511...' }
    Usamos o telefone do INPUT (Claude pode ter normalizado) com fallback
    para o telefone da sessão (origem do WhatsApp)."""
    from zapi_adapter import phone_variants

    phone_raw = (input_data or {}).get('phone') or context.get('phone') or ''
    variants = phone_variants(phone_raw)

    if not variants:
        return {'error': 'phone vazio ou invalido', 'orders': []}

    if not shopify_client.is_configured():
        return {'error': 'shopify nao configurado no servidor', 'orders': []}

    customers = shopify_client.search_customers_by_phone(variants)
    if not customers:
        return {
            'orders': [],
            'message': 'Nenhum cliente encontrado com esse telefone na loja.',
            'tried_variants': variants,
        }

    # Pega pedidos do(s) cliente(s) encontrado(s) — pode haver duplicatas, agregamos
    seen = set()
    summaries = []
    for c in customers[:3]:  # no máximo 3 customers para evitar custo excessivo
        cid = c.get('id')
        if not cid:
            continue
        orders = shopify_client.list_customer_orders(cid, limit=5)
        for o in orders:
            oid = o.get('id')
            if oid in seen:
                continue
            seen.add(oid)
            s = shopify_client.summarize_order(o)
            if s:
                summaries.append(s)

    # Ordena pelo mais recente
    summaries.sort(key=lambda x: x.get('created_at') or '', reverse=True)

    return {
        'orders': summaries[:5],
        'customer_name': customers[0].get('first_name', '') + ' ' + customers[0].get('last_name', ''),
        'customer_id': customers[0].get('id'),
        'total_found': len(summaries),
    }


def _tool_buscar_pedido_por_numero(input_data, context):
    order_number = (input_data or {}).get('order_number')
    if not order_number:
        return {'error': 'order_number obrigatorio', 'order': None}

    if not shopify_client.is_configured():
        return {'error': 'shopify nao configurado no servidor', 'order': None}

    o = shopify_client.get_order_by_number(order_number)
    if not o:
        return {
            'order': None,
            'message': f'Pedido {order_number} nao encontrado.',
        }
    return {'order': shopify_client.summarize_order(o)}


def _tool_escalar_para_humano(input_data, context):
    """Marca a sessão como handoff e cria registro em atendente_handoff.
    O envio efetivo de notificação para a equipe (e-mail/Telegram) fica para
    a Fase 3."""
    from models import db, AtendenteHandoff

    session = context.get('session')
    if not session:
        return {'error': 'sessao nao encontrada no contexto', 'escalated': False}

    motivo = (input_data or {}).get('motivo') or 'sem motivo informado'
    resumo = (input_data or {}).get('resumo') or ''

    handoff = AtendenteHandoff(
        session_id=session.id,
        reason=motivo[:255],
        summary=resumo,
        status='pending',
    )
    session.status = 'handoff'

    db.session.add(handoff)
    db.session.commit()

    return {
        'escalated': True,
        'handoff_id': handoff.id,
        'message': 'Sessao marcada para atendimento humano.',
    }


TOOL_IMPL = {
    'buscar_pedido_por_telefone': _tool_buscar_pedido_por_telefone,
    'buscar_pedido_por_numero': _tool_buscar_pedido_por_numero,
    'escalar_para_humano': _tool_escalar_para_humano,
}


def run_tool(name, input_data, context):
    """Executa uma tool com tratamento de erro homogêneo. Retorna sempre dict."""
    impl = TOOL_IMPL.get(name)
    if not impl:
        return {'error': f'tool desconhecida: {name}'}
    try:
        return impl(input_data, context)
    except Exception as e:
        # Erro inesperado vira erro estruturado pro agente — ele decide se
        # escala ou tenta outra abordagem.
        return {'error': f'falha na execucao da tool: {type(e).__name__}: {e}'}
