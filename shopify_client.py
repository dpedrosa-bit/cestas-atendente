"""
shopify_client.py - Cliente leve para a Admin API da Shopify

Usa REST por simplicidade (igual ao cestas-routes). Token estático via
SHOPIFY_TOKEN. Para a fase 2 podemos migrar para o fluxo OAuth Client
Credentials usado no cestas-company, ou pra GraphQL pra buscas mais
flexiveis, mas para o MVP REST + multiplas variantes resolve.

API version travada em 2024-10 (igual cestas-routes).
"""
import os
import re
import unicodedata
import logging
import requests

logger = logging.getLogger(__name__)

SHOPIFY_DOMAIN = os.environ.get('SHOPIFY_DOMAIN', '')
SHOPIFY_TOKEN = os.environ.get('SHOPIFY_TOKEN', '')
SHOPIFY_API_VERSION = os.environ.get('SHOPIFY_API_VERSION', '2024-10')
# Prefixo da numeracao da loja (ex: "CC" para Cestas Company -> pedidos #CC3752).
# Vazio para lojas sem prefixo. Cliente pode digitar com ou sem o prefixo.
SHOPIFY_ORDER_PREFIX = os.environ.get('SHOPIFY_ORDER_PREFIX', '').strip().upper()
# Dominio publico da loja (frente Shopify) — usado pra montar link de produto.
# Cestas Company: cestascompany.com.br. Quando Flower entrar: setar env.
PUBLIC_DOMAIN = os.environ.get('CESTAS_PUBLIC_DOMAIN', 'cestascompany.com.br').strip()


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
            logger.warning(f'[shopify] customer search HTTP {r.status_code} query={query!r}: {r.text[:200]}')
            return []
        customers = r.json().get('customers', []) or []
        logger.info(f'[shopify] customer search query={query!r} → {len(customers)} hit(s)')
        return customers
    except requests.exceptions.RequestException as e:
        logger.warning(f'[shopify] customer search request error: {e}')
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
    """Busca um pedido pelo número. Aceita varios formatos como o cliente
    pode digitar no WhatsApp:
      - "CC3752"   -> busca name=#CC3752
      - "Cc3752"   -> busca name=#CC3752 (case-insensitive)
      - "#CC3752"  -> busca name=#CC3752
      - "3752"     -> busca name=#CC3752 (prepende prefixo da env), fallback #3752
      - "#3752"    -> idem
    Retorna o pedido (dict) ou None se nao achar."""
    if not is_configured() or not order_number:
        return None

    raw = str(order_number).strip().lstrip('#').upper()
    if not raw:
        return None

    has_letters = any(c.isalpha() for c in raw)

    # Lista de nomes candidatos a tentar (ordem importa: mais especifico primeiro).
    # Cobrimos os 3 padroes que vimos na pratica:
    #   - "#CC3752"  (padrao Shopify default)
    #   - "CC3752"   (lojas que customizam ordem e removem o #)
    #   - "cc3752"   (variantes em caixa baixa — algumas lojas)
    bare_candidates = []
    if has_letters:
        bare_candidates.append(raw)  # "CC3752"
    else:
        if SHOPIFY_ORDER_PREFIX:
            bare_candidates.append(f'{SHOPIFY_ORDER_PREFIX}{raw}')  # "CC3752"
        bare_candidates.append(raw)  # "3752"

    # Para cada bare, gera 4 variantes de busca
    candidates = []
    for bare in bare_candidates:
        candidates.append(f'#{bare}')        # #CC3752
        candidates.append(bare)              # CC3752
        candidates.append(f'#{bare.lower()}')# #cc3752
        candidates.append(bare.lower())      # cc3752

    fields = (
        'id,order_number,name,created_at,financial_status,fulfillment_status,'
        'cancelled_at,cancel_reason,total_price,currency,'
        'tags,note,note_attributes,shipping_address,line_items,customer'
    )

    last_error = None
    for name in candidates:
        url = f'{_base()}/orders.json'
        params = {'status': 'any', 'name': name, 'limit': 1, 'fields': fields}
        try:
            r = requests.get(url, headers=_headers(), params=params, timeout=timeout)
            if r.status_code >= 400:
                logger.warning(f'[shopify] order search HTTP {r.status_code} for name={name!r}: {r.text[:200]}')
                last_error = f'HTTP {r.status_code}'
                continue
            orders = r.json().get('orders', []) or []
            logger.info(f'[shopify] order search name={name!r} → {len(orders)} hit(s)')
            if orders:
                return orders[0]
        except requests.exceptions.RequestException as e:
            logger.warning(f'[shopify] order search request error for name={name!r}: {e}')
            last_error = str(e)
            continue

    logger.info(f'[shopify] order {order_number!r} nao encontrado apos {len(candidates)} tentativas (ultimo erro: {last_error})')
    return None


def list_recent_orders_sample(limit=5, timeout=15):
    """Diagnostico: lista os N pedidos mais recentes da loja com o campo
    `name` exposto, pra a gente conferir formato real (com/sem #, com/sem
    prefixo, etc). Nao usado no fluxo normal — so via /debug/shopify-sample.
    """
    if not is_configured():
        return {'error': 'shopify not configured'}

    url = f'{_base()}/orders.json'
    params = {
        'status': 'any',
        'limit': max(1, min(int(limit), 25)),
        'fields': 'id,order_number,name,created_at,financial_status,fulfillment_status,tags',
    }

    try:
        r = requests.get(url, headers=_headers(), params=params, timeout=timeout)
        if r.status_code >= 400:
            return {
                'error': f'HTTP {r.status_code}',
                'detail': r.text[:500],
                'domain': SHOPIFY_DOMAIN,
                'api_version': SHOPIFY_API_VERSION,
            }
        orders = r.json().get('orders', []) or []
        return {
            'count': len(orders),
            'orders': orders,
            'domain': SHOPIFY_DOMAIN,
            'api_version': SHOPIFY_API_VERSION,
            'configured_prefix': SHOPIFY_ORDER_PREFIX,
        }
    except requests.exceptions.RequestException as e:
        return {'error': str(e)}


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


# ─────────────────────────────────────────────────────────────────────────────
# Busca de produtos (Sprint 5.2 — tool buscar_produtos)
# ─────────────────────────────────────────────────────────────────────────────

# Palavras-chave que indicam produto auxiliar (frete, taxa) — nao devem ser
# sugeridos ao cliente como cesta/presente.
_PRODUCT_EXCLUDE_KEYWORDS = ('frete', 'shipping', 'taxa de entrega', 'envio')


def _is_excluded_product(product):
    """Heuristica simples: exclui produtos auxiliares do catalogo nas sugestoes."""
    title = (product.get('title') or '').lower()
    ptype = (product.get('product_type') or '').lower()
    if ptype in ('frete', 'shipping'):
        return True
    for kw in _PRODUCT_EXCLUDE_KEYWORDS:
        if kw in title:
            return True
    return False


def _strip_accents(s):
    """Remove acentos pra gerar variante da query (aniversário -> aniversario)."""
    if not s:
        return s
    nfkd = unicodedata.normalize('NFKD', s)
    return ''.join(c for c in nfkd if not unicodedata.combining(c))


def _query_variants(query):
    """Gera ate 4 variantes da query pro filtro `title=` do Shopify REST.
    O endpoint faz match parcial case-insensitive mas e sensivel a acentos —
    entao precisamos tentar com e sem acentuacao.
    Ex: 'Aniversário' -> ['Aniversario', 'Aniversário', 'aniversario', 'aniversario'].
    """
    q = (query or '').strip()
    if not q:
        return []
    no_accent = _strip_accents(q)
    out = []
    seen = set()
    for v in (q, no_accent, q.lower(), no_accent.lower()):
        v = (v or '').strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


_PRODUCT_SEARCH_GQL = """
query SearchProducts($q: String!, $first: Int!, $sortKey: ProductSortKeys, $reverse: Boolean) {
  products(first: $first, query: $q, sortKey: $sortKey, reverse: $reverse) {
    edges {
      node {
        id
        title
        handle
        productType
        vendor
        tags
        status
        descriptionHtml
        featuredImage { url }
        variants(first: 5) {
          edges { node { id price } }
        }
      }
    }
  }
}
"""

# Mapeamento de ordenacao amigavel -> ProductSortKeys do Shopify GraphQL
_GQL_SORT_MAP = {
    'best_selling': ('BEST_SELLING', False),  # mais vendidos primeiro
    'price_asc':    ('PRICE', False),          # mais baratos primeiro
    'price_desc':   ('PRICE', True),           # mais caros primeiro
    'relevance':    ('RELEVANCE', False),      # algoritmo Shopify
    'created_desc': ('CREATED_AT', True),      # mais recentes primeiro
}


def _gql_sort_params(sort_label):
    """Resolve sortKey + reverse pro Shopify a partir de um label simples."""
    return _GQL_SORT_MAP.get(sort_label, _GQL_SORT_MAP['best_selling'])


def _slugify(s):
    """Converte 'Aniversário Premium' -> 'aniversario-premium'. Usado pra
    montar tags de destaque (ex: 'dest-aniversario')."""
    if not s:
        return ''
    txt = _strip_accents(s).lower()
    txt = re.sub(r'[^a-z0-9]+', '-', txt)
    return txt.strip('-')


def _graphql(query_str, variables=None, timeout=15):
    """Executa query GraphQL na Admin API. Retorna (data, error_msg).
    error_msg eh None em caso de sucesso."""
    url = f'https://{SHOPIFY_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/graphql.json'
    body = {'query': query_str}
    if variables:
        body['variables'] = variables
    try:
        r = requests.post(url, headers=_headers(), json=body, timeout=timeout)
        if r.status_code >= 400:
            return None, f'HTTP {r.status_code}: {r.text[:300]}'
        payload = r.json()
        if payload.get('errors'):
            return None, str(payload['errors'])[:300]
        return payload.get('data'), None
    except requests.exceptions.RequestException as e:
        return None, f'request error: {e}'


def _gql_node_to_rest(node):
    """Converte node GraphQL pro shape REST-like que summarize_product espera."""
    fi = node.get('featuredImage') or {}
    images = [{'src': fi.get('url')}] if fi.get('url') else []

    variants = []
    v_edges = ((node.get('variants') or {}).get('edges')) or []
    for ve in v_edges:
        v = ve.get('node') or {}
        variants.append({'price': v.get('price')})

    return {
        'id': node.get('id'),
        'title': node.get('title') or '',
        'handle': node.get('handle') or '',
        'product_type': node.get('productType') or '',
        'vendor': node.get('vendor') or '',
        'tags': ','.join(node.get('tags') or []),
        'images': images,
        'variants': variants,
        'body_html': node.get('descriptionHtml') or '',
    }


def _run_gql_search(search_q, first, sort_key, reverse, timeout):
    """Executa uma busca GraphQL e retorna lista de nodes (ou [] em erro)."""
    data, err = _graphql(
        _PRODUCT_SEARCH_GQL,
        variables={
            'q': search_q,
            'first': first,
            'sortKey': sort_key,
            'reverse': reverse,
        },
        timeout=timeout,
    )
    if err:
        logger.warning(f'[shopify] gql search q={search_q!r} error: {err}')
        return []
    edges = ((data or {}).get('products') or {}).get('edges') or []
    logger.info(f'[shopify] gql search q={search_q!r} sort={sort_key}/{reverse} '
                f'→ {len(edges)} hit(s)')
    return [edge.get('node') for edge in edges if edge.get('node')]


def search_products(query, max_results=5, sort='best_selling', timeout=15):
    """Busca produtos via GraphQL Admin API com priorizacao em camadas:

    1. PRIORIDADE: produtos com tag `dest-<slug>` (ex: dest-aniversario) —
       voce marca no admin Shopify os destaques curados pra cada ocasiao.
    2. FALLBACK: busca full-text na query original (title + vendor +
       product_type + tags do produto).

    Tudo ordenado por `sort` (best_selling default).

    Args:
        query: termo da busca ("vinho", "aniversario", "mae"...)
        max_results: max produtos retornados (default 5)
        sort: best_selling | price_asc | price_desc | relevance | created_desc
    """
    if not is_configured() or not query:
        return []

    variants = _query_variants(query)
    if not variants:
        return []

    sort_key, reverse = _gql_sort_params(sort)
    api_first = max(5, min(int(max_results) * 3, 25))

    seen_ids = set()
    accumulated = []

    def _append_nodes(nodes):
        for node in nodes:
            pid = node.get('id')
            if not pid or pid in seen_ids:
                continue
            seen_ids.add(pid)
            product = _gql_node_to_rest(node)
            if _is_excluded_product(product):
                continue
            accumulated.append(product)
            if len(accumulated) >= max_results:
                return True
        return False

    # FASE 1: tag de destaque curada pelo operador
    slug = _slugify(query)
    if slug:
        boost_tag = f'dest-{slug}'
        boost_q = f'tag:{boost_tag} status:active'
        nodes = _run_gql_search(boost_q, api_first, sort_key, reverse, timeout)
        if nodes:
            logger.info(f'[shopify] boost tag {boost_tag!r}: {len(nodes)} produto(s)')
        if _append_nodes(nodes):
            return accumulated

    # FASE 2: busca livre full-text (title + tags + vendor + product_type)
    for v in variants:
        if len(accumulated) >= max_results:
            break
        search_q = f'{v} status:active'
        nodes = _run_gql_search(search_q, api_first, sort_key, reverse, timeout)
        if _append_nodes(nodes):
            break

    logger.info(f'[shopify] search query={query!r} sort={sort!r} final: '
                f'{len(accumulated)} produto(s)')
    return accumulated


def _strip_html(html):
    """Remove tags HTML e normaliza espacos. Pra encurtar descricoes longas."""
    if not html:
        return ''
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def summarize_product(product):
    """Resume um produto pro tool result — campos essenciais pro cliente."""
    if not product:
        return None

    variants = product.get('variants') or []
    price_label = ''
    price_min = None
    price_max = None
    if variants:
        prices = []
        for v in variants:
            try:
                p = float(v.get('price') or 0)
                if p > 0:
                    prices.append(p)
            except (TypeError, ValueError):
                continue
        if prices:
            price_min = min(prices)
            price_max = max(prices)
            if abs(price_max - price_min) < 0.01:
                price_label = f'R$ {price_min:.2f}'.replace('.', ',')
            else:
                price_label = (f'R$ {price_min:.2f} a R$ {price_max:.2f}'
                                .replace('.', ','))

    images = product.get('images') or []
    image_url = images[0].get('src') if images else None

    description = _strip_html(product.get('body_html') or '')
    if len(description) > 240:
        description = description[:237].rstrip() + '...'

    handle = product.get('handle') or ''
    link = f'https://{PUBLIC_DOMAIN}/products/{handle}' if (handle and PUBLIC_DOMAIN) else None

    return {
        'title': product.get('title'),
        'handle': handle,
        'link': link,
        'price': price_label,
        'price_min': price_min,
        'price_max': price_max,
        'image_url': image_url,
        'description': description,
        'product_type': product.get('product_type'),
        'vendor': product.get('vendor'),
    }
