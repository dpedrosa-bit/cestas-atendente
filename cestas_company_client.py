"""
cestas_company_client.py - Cliente HTTP server-to-server pro cestas-company

Consome endpoints internos do cestas-company que requerem bearer token
(INTERNAL_API_TOKEN). Por enquanto so o /api/atendente/slots (Sprint 2),
mas e o lugar pra adicionar futuros endpoints do tipo /api/atendente/*.

REGRA DE OURO: nunca duplicar logica que vive no cestas-company.
Esse cliente eh transporte puro — toda regra de negocio fica do outro lado.
"""
import os
import logging
import requests

logger = logging.getLogger(__name__)

CESTAS_COMPANY_URL = os.environ.get(
    'CESTAS_COMPANY_URL',
    'https://cestas-company-production.up.railway.app',
).rstrip('/')

INTERNAL_API_TOKEN = os.environ.get('INTERNAL_API_TOKEN', '').strip()
# Shop padrao quando a tool nao especifica — usamos o domain Shopify ja
# configurado pra o atendente (mesma loja que o SHOPIFY_DOMAIN aponta).
DEFAULT_SHOP = os.environ.get('SHOPIFY_DOMAIN', '').strip()


def is_configured():
    return bool(CESTAS_COMPANY_URL and INTERNAL_API_TOKEN)


def _headers():
    return {
        'Authorization': f'Bearer {INTERNAL_API_TOKEN}',
        'Accept': 'application/json',
    }


def get_delivery_slots(cep, shop=None, days_ahead=7, timeout=15):
    """Consulta disponibilidade de entrega pro CEP informado.

    Args:
        cep: CEP (string com ou sem mascara — o cestas-company normaliza)
        shop: dominio Shopify (default: SHOPIFY_DOMAIN da env)
        days_ahead: 0-14, quantos dias alem de hoje consultar (default 7)
        timeout: timeout HTTP em segundos

    Retorna dict com a resposta do endpoint, ou dict com 'error' se falhar.
    Estrutura esperada em sucesso:
        {
          'available': bool,
          'cep': '01310100',
          'logradouro': 'Av. Paulista',
          'bairro': 'Bela Vista',
          'cidade': 'São Paulo',
          'estado': 'SP',
          'distance_km': 2.5,
          'range_label': 'Centro Expandido',
          'region_label': '...',
          'current_time_brt': '2026-05-13T14:30:00-03:00',
          'max_days_ahead': 90,
          'days': [
            {
              'date': '2026-05-13', 'day_name': 'hoje (qua)', 'blocked': False,
              'slots': [
                {'label': 'Tarde', 'delivery_type_label': 'Manha (08-13)',
                 'price_label': 'R$ 8,00', 'available': True, 'reason': None, ...}
              ]
            }
          ]
        }
    """
    if not is_configured():
        return {'error': 'cestas_company_client nao configurado (INTERNAL_API_TOKEN ou CESTAS_COMPANY_URL ausente)'}

    if not cep or not str(cep).strip():
        return {'error': 'cep vazio'}

    params = {'cep': str(cep).strip(), 'days_ahead': str(int(days_ahead))}
    if shop or DEFAULT_SHOP:
        params['shop'] = shop or DEFAULT_SHOP

    url = f'{CESTAS_COMPANY_URL}/api/atendente/slots'

    try:
        r = requests.get(url, headers=_headers(), params=params, timeout=timeout)
        if r.status_code == 401:
            logger.error('[cestas-company] 401 — INTERNAL_API_TOKEN nao bate entre os apps')
            return {'error': 'auth_failed', 'message': 'token interno invalido'}
        if r.status_code == 503:
            logger.error('[cestas-company] 503 — INTERNAL_API_TOKEN nao configurado no servidor')
            return {'error': 'server_misconfigured', 'message': 'token interno nao setado no cestas-company'}
        if r.status_code >= 400:
            logger.warning(f'[cestas-company] HTTP {r.status_code}: {r.text[:300]}')
            return {'error': f'http_{r.status_code}', 'detail': r.text[:500]}
        data = r.json()
        logger.info(
            f'[cestas-company] slots cep={cep} available={data.get("available")} '
            f'days={len(data.get("days") or [])}'
        )
        return data
    except requests.exceptions.RequestException as e:
        logger.warning(f'[cestas-company] erro requisicao: {e}')
        return {'error': 'request_error', 'message': str(e)}
