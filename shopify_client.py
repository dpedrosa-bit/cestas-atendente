"""
shopify_client.py - Cliente leve para a Admin API da Shopify

Usa REST por simplicidade (igual ao cestas-routes). Token estático via
SHOPIFY_TOKEN. Para a fase 2 podemos migrar para o fluxo OAuth Client
Credentials usado no cestas-company, mas para o MVP não vale a pena a
complexidade adicional.

API version travada em 2024-10 (igual cestas-routes).
"""
import os
import requests

SHOPIFY_DOMAIN = os.environ.get('SHOPIFY_DOMAIN', '')
SHOPIFY_TOKEN = os.environ.get('SHOPIFY_TOKEN', '')
SHOPIFY_API_VERSION = os.environ.get('SHOPIFY_API_VERSION', '2024-10')


def is_configured():
    return bool(SHOPIFY_DOMAIN and SHOPIFY_TOKEN)


def _headers():
    return {
        'X-Shopify-Access-Token': SHOPIFY_TOKEN,
        'Content-Type': 'application/json',
    }


def _base():
    return f'https://{SHOPIFY_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}'


def search_customers_by_phone(phone_variants, timeout=15):
    """Busca clientes na Shopify cuja propriedade `phone` (ou default_address.phone)
    bata com qualquer uma das variantes. Retorna lista de customers (max 5)."""
    if not is_configured() or not phone_variants:
        return []

    # Shopify suporta busca por múltiplos termos com OR no query string.
    # Construímos: "phone:'5511999...' OR phone:'+5511999...' OR phone:'1199...'"
    query = ' OR '.join([f"phone:{v}" for v in phone_variants[:8]])
    url = f'{_base()}/customers/search.json'
    params = {'query': query, 'limit': 5}

    try:
        r = requests.get(url, headers=_headers(), params=params, timeout=timeout)
        if r.status_code >= 400:
            return []
        return r.json().get('customers', []) or []
    except requests.exceptions.RequestException:
        return []


def list_customer_orders(customer_id, limit=5, timeout=15):
    """Últimos N pedidos de um cliente. Usa endpoint /customers/{id}/orders.json
    com status=any para incluir pedidos cancelados/recusados (atendimento
    pode estar tirando dúvida de pedido cancelado também)."""
    if not is_configured() or not customer_id:
        return []

    url = f'{_base()}/customers/{customer_id}/orders.json'
    params = {
        'status': 'any',
        'limit': limit,
        'fields': (
            'id,order_number,name,created_at,financial_status,fulfillment_status,'
            'cancelled_at,cancel_reason,total_price,currency,'
            'tags,note,note_attributes,shipping_address,line_items'
        ),
    }

    try:
        r = requests.get(url, headers=_headers(), params=params, timeout=timeout)
        if r.status_code >= 400:
            return []
        return r.json().get('orders', []) or []
    except requests.exceptions.RequestException:
        return []


def get_order_by_number(order_number, timeout=15):
    """Busca um pedido pelo número (ex: 12345 ou #12345 ou CC12345).
    Retorna o pedido ou None."""
    if not is_configured() or not order_number:
        return None

    # Limpeza: aceita "CC12345", "#12345", "12345"
    raw = str(order_number).strip()
    digits_only = ''.join(ch for ch in raw if ch.isdigit())
    if not digits_only:
        return None

    # Shopify aceita busca por `name` (ex: "#1001") ou `order_number` numérico.
    url = f'{_base()}/orders.json'
    params = {
        'status': 'any',
        'name': f'#{digits_only}',
        'limit': 1,
        'fields': (
            'id,order_number,name,created_at,financial_status,fulfillment_status,'
            'cancelled_at,cancel_reason,total_price,currency,'
            'tags,note,note_attributes,shipping_address,line_items,customer'
        ),
    }

    try:
        r = requests.get(url, headers=_headers(), params=params, timeout=timeout)
        if r.status_code >= 400:
            return None
        orders = r.json().get('orders', []) or []
        return orders[0] if orders else None
    except requests.exceptions.RequestException:
        return None


def extract_attrs(order):
    """Mescla note_attributes (legacy) + line_items[].properties (widget novo).
    Igual à função extractAttrs do cestas-routes (server.js:477).

    Pedidos antigos gravam dados de presente/horário em note_attributes;
    o widget atual grava em properties dos line items.
    """
    out = {}

    # 1. line_items[].properties (widget v2, mais comum hoje)
    for item in order.get('line_items') or []:
        for prop in item.get('properties') or []:
            name = prop.get('name')
            value = prop.get('value')
            if name and value is not None and name not in out:
                out[name] = value

    # 2. note_attributes sobrescreve (caminho oficial Shopify, autoritário)
    for attr in order.get('note_attributes') or []:
        name = attr.get('name')
        value = attr.get('value')
        if name and value is not None:
            out[name] = value

    return out


def summarize_order(order):
    """Resume um pedido Shopify em dict compacto pronto para o agente IA
    consumir. Foco no que um cliente pergunta no WhatsApp."""
    if not order:
        return None

    attrs = extract_attrs(order)
    addr = order.get('shipping_address') or {}

    # Itens (sem cartões/complementos visualmente — agente decide o que mostrar)
    items = []
    for li in (order.get('line_items') or []):
        title = li.get('title') or ''
        qty = li.get('quantity') or 1
        items.append({
            'title': title,
            'quantity': qty,
            'sku': li.get('sku') or '',
        })

    # Status humano (Shopify usa termos em inglês)
    fin = order.get('financial_status') or ''
    ful = order.get('fulfillment_status') or 'unfulfilled'
    cancelled = bool(order.get('cancelled_at'))

    return {
        'order_number': order.get('order_number'),
        'name': order.get('name'),  # ex: "#CC12345"
        'created_at': order.get('created_at'),
        'financial_status': fin,
        'fulfillment_status': ful,
        'cancelled': cancelled,
        'cancel_reason': order.get('cancel_reason'),
        'total': f"{order.get('currency','BRL')} {order.get('total_price','')}".strip(),
        'tags': order.get('tags') or '',
        'shipping_to': {
            'name': f"{addr.get('first_name','')} {addr.get('last_name','')}".strip(),
            'city': addr.get('city') or '',
            'province': addr.get('province') or '',
            'zip': addr.get('zip') or '',
            'address1': addr.get('address1') or '',
            'phone': addr.get('phone') or '',
        },
        # Dados do widget de entrega (chaves que o widget grava)
        'delivery_date': attrs.get('Data de entrega') or attrs.get('delivery_date') or '',
        'delivery_slot': attrs.get('Horário') or attrs.get('horario') or attrs.get('Janela') or '',
        'delivery_type': attrs.get('Tipo') or attrs.get('Tipo de entrega') or '',
        'gift_to': attrs.get('Para') or attrs.get('Destinatário') or '',
        'gift_from': attrs.get('De') or attrs.get('Remetente') or '',
        'gift_message': attrs.get('Mensagem') or attrs.get('Mensagem de presente') or '',
        'items': items,
        'note': order.get('note') or '',
    }
