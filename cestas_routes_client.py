"""
cestas_routes_client.py - Cliente HTTP server-to-server pro cestas-routes

Consome endpoints internos do cestas-routes que requerem bearer token
(INTERNAL_API_TOKEN). Sprint 3: apenas /api/atendente/order-status, que
retorna timeline de status completa de UM pedido (envHistory + prodHistory
parseados dos note_attributes SE_/SP_ do Shopify).

REGRA DE OURO: nunca duplicar logica que vive no cestas-routes. Esse
cliente eh transporte puro — toda regra de negocio (lookup, normalizacao
de status, parse de timeline) fica do outro lado.
"""
import os
import logging
import requests

logger = logging.getLogger(__name__)

CESTAS_ROUTES_URL = os.environ.get(
    'CESTAS_ROUTES_URL',
    'https://cestas-routes-production.up.railway.app',
).rstrip('/')

INTERNAL_API_TOKEN = os.environ.get('INTERNAL_API_TOKEN', '').strip()


def is_configured():
    return bool(CESTAS_ROUTES_URL and INTERNAL_API_TOKEN)


def _headers():
    return {
        'Authorization': f'Bearer {INTERNAL_API_TOKEN}',
        'Accept': 'application/json',
    }


def get_order_status(order_number, timeout=15):
    """Consulta status completo de um pedido pelo numero (CC15377, #15377 ou 15377).

    Args:
        order_number: numero do pedido como o cliente informou
        timeout: timeout HTTP em segundos

    Retorna dict com a resposta do endpoint, ou dict com 'error' se falhar.
    Estrutura esperada em sucesso:
        {
          'ok': True,
          'id': 'CC15377',
          'orderNumber': 15377,
          'createdAt': '2026-05-13T07:00:00-03:00',
          'currentEnv': 'Pedido em Trânsito',
          'currentProd': 'Roteiro Separado',
          'envHistory': [{'status': 'Aprovado', 'at': '2026-05-13 07:00:00'}, ...],
          'prodHistory': [{'status': 'Aguardando Produção', 'at': '...'}, ...],
          'timeline_unavailable': False,
          'incomplete': False,
          'incompleteIssues': [],
          'customer': {'name': '...', 'phone': '...', 'email': '...'},
          'address': {'street': '...', 'city': '...', 'zip': '...'},
          'delivery': {'date': '2026-05-13', 'slot': '08:00 - 12:00', 'type': 'Manhã', ...}
        }

    Erros possiveis:
        {'error': 'not_found', ...}        — pedido nao existe nos ultimos 60 dias
        {'error': 'unauthorized', ...}     — INTERNAL_API_TOKEN nao bate
        {'error': 'request_error', ...}    — falha de rede
        {'error': 'http_XXX', ...}         — outro erro HTTP
    """
    if not is_configured():
        return {
            'error': 'cestas_routes_client_nao_configurado',
            'message': 'INTERNAL_API_TOKEN ou CESTAS_ROUTES_URL ausente',
        }

    if not order_number or not str(order_number).strip():
        return {'error': 'order_number_vazio'}

    url = f'{CESTAS_ROUTES_URL}/api/atendente/order-status'
    params = {'order': str(order_number).strip()}

    try:
        r = requests.get(url, headers=_headers(), params=params, timeout=timeout)
        if r.status_code == 401:
            logger.error('[cestas-routes] 401 — INTERNAL_API_TOKEN nao bate entre os apps')
            return {'error': 'auth_failed', 'message': 'token interno invalido'}
        if r.status_code == 503:
            logger.error('[cestas-routes] 503 — INTERNAL_API_TOKEN nao configurado no servidor')
            return {'error': 'server_misconfigured', 'message': 'token interno nao setado no cestas-routes'}
        if r.status_code == 404:
            data = r.json() if r.content else {}
            return {
                'error': 'not_found',
                'order': data.get('order') or order_number,
                'message': data.get('message') or 'Pedido nao encontrado nos ultimos 60 dias.',
            }
        if r.status_code >= 400:
            logger.warning(f'[cestas-routes] HTTP {r.status_code}: {r.text[:300]}')
            return {'error': f'http_{r.status_code}', 'detail': r.text[:500]}
        data = r.json()
        logger.info(
            f'[cestas-routes] order={order_number} '
            f'env={data.get("currentEnv")} prod={data.get("currentProd")} '
            f'timeline_unavailable={data.get("timeline_unavailable")}'
        )
        return data
    except requests.exceptions.RequestException as e:
        logger.warning(f'[cestas-routes] erro requisicao: {e}')
        return {'error': 'request_error', 'message': str(e)}
