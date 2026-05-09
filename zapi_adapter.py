"""
zapi_adapter.py - Adapter para Z-API (https://z-api.io)

Responsabilidades:
1. Parse do webhook recebido (apenas mensagens de texto de clientes; ignora
   eventos de status, mensagens enviadas pela própria conta, e callbacks de
   presença).
2. Envio de mensagens de texto para o cliente via API REST.

Headers obrigatórios em TODAS as chamadas: `Client-Token: {ZAPI_CLIENT_TOKEN}`.
Esquecer esse header é o erro #1 com Z-API — retorna 401 silenciosamente.

Documentação: https://developer.z-api.io
"""
import os
import re
import requests

ZAPI_INSTANCE_ID = os.environ.get('ZAPI_INSTANCE_ID', '')
ZAPI_INSTANCE_TOKEN = os.environ.get('ZAPI_INSTANCE_TOKEN', '')
ZAPI_CLIENT_TOKEN = os.environ.get('ZAPI_CLIENT_TOKEN', '')

ZAPI_BASE = f'https://api.z-api.io/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_INSTANCE_TOKEN}'


def is_configured():
    return bool(ZAPI_INSTANCE_ID and ZAPI_INSTANCE_TOKEN and ZAPI_CLIENT_TOKEN)


def _headers():
    return {
        'Content-Type': 'application/json',
        'Client-Token': ZAPI_CLIENT_TOKEN,
    }


def normalize_phone(phone: str) -> str:
    """Remove tudo que não é dígito. Z-API usa formato E.164 sem o '+'.
    Ex: '+55 (11) 99999-1234' -> '5511999991234'"""
    if not phone:
        return ''
    return re.sub(r'\D', '', phone)


def phone_variants(phone: str):
    """Gera variações do número para matching contra Shopify, que pode ter
    o telefone gravado em vários formatos diferentes. Retorna lista única
    preservando ordem (do mais específico ao mais genérico)."""
    digits = normalize_phone(phone)
    if not digits:
        return []

    variants = []

    def add(v):
        if v and v not in variants:
            variants.append(v)

    # Como recebido (E.164 sem +)
    add(digits)
    # Com '+' na frente
    add('+' + digits)

    # Brasil — heurísticas comuns
    if digits.startswith('55') and len(digits) >= 12:
        local = digits[2:]  # sem código do país
        add('+55' + local)
        add('55' + local)
        add(local)
        # Sem o nono dígito (celular antigo): 5511999991234 -> 511199991234
        if len(local) == 11:  # DDD (2) + 9 + 8 dígitos
            without_9 = local[:2] + local[3:]
            add('55' + without_9)
            add('+55' + without_9)
            add(without_9)
        # Com o nono dígito quando faltou
        elif len(local) == 10:
            with_9 = local[:2] + '9' + local[2:]
            add('55' + with_9)
            add('+55' + with_9)
            add(with_9)

    return variants


def parse_webhook(payload: dict):
    """Recebe o JSON do webhook Z-API. Retorna dict com:
        { 'phone': str, 'name': str, 'text': str, 'message_id': str, 'is_group': bool }
    ou None se a mensagem deve ser IGNORADA (status, fromMe, grupo, áudio,
    mídia sem texto, etc). MVP só responde texto puro de chats individuais.

    Tipos de evento Z-API mais comuns:
      - ReceivedCallback         -> mensagem recebida (pode ser texto/imagem/áudio/etc)
      - DeliveryCallback         -> entrega/leitura — IGNORAR
      - MessageStatusCallback    -> status do envio — IGNORAR
      - PresenceChatCallback     -> digitando/online — IGNORAR
    """
    if not isinstance(payload, dict):
        return None

    # Mensagens enviadas por nós mesmos (eco) — ignorar
    if payload.get('fromMe'):
        return None

    # Eventos que não são mensagem recebida
    event_type = payload.get('type') or payload.get('event')
    if event_type and event_type != 'ReceivedCallback':
        return None

    phone = payload.get('phone') or ''
    if not phone:
        return None

    is_group = bool(payload.get('isGroup'))
    if is_group:
        # MVP não atende grupos
        return None

    # Z-API pode entregar texto em vários formatos dependendo do tipo
    text = ''
    if isinstance(payload.get('text'), dict):
        text = payload['text'].get('message', '') or ''
    elif isinstance(payload.get('text'), str):
        text = payload['text']

    # Mensagem de botão (lista, quick reply, etc)
    if not text and isinstance(payload.get('buttonReply'), dict):
        text = payload['buttonReply'].get('message', '')
    if not text and isinstance(payload.get('listResponseMessage'), dict):
        text = payload['listResponseMessage'].get('message', '')

    if not text:
        # Áudio, imagem, vídeo, sticker — fora do escopo do MVP
        return None

    return {
        'phone': normalize_phone(phone),
        'name': payload.get('senderName') or payload.get('chatName') or '',
        'text': text.strip(),
        'message_id': payload.get('messageId') or payload.get('id') or '',
        'is_group': False,
    }


def send_text(phone: str, message: str, timeout: int = 15):
    """Envia mensagem de texto. Retorna (ok, response_dict_or_error_str)."""
    if not is_configured():
        return False, 'Z-API nao configurado (ZAPI_INSTANCE_ID/TOKEN/CLIENT_TOKEN)'

    phone_normalized = normalize_phone(phone)
    if not phone_normalized:
        return False, 'phone vazio apos normalizacao'

    if not message or not message.strip():
        return False, 'message vazia'

    url = f'{ZAPI_BASE}/send-text'
    body = {'phone': phone_normalized, 'message': message.strip()}

    try:
        r = requests.post(url, json=body, headers=_headers(), timeout=timeout)
        if r.status_code >= 400:
            return False, f'HTTP {r.status_code}: {r.text[:500]}'
        return True, r.json() if r.text else {}
    except requests.exceptions.RequestException as e:
        return False, f'request error: {e}'
