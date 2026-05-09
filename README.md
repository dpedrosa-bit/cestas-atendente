# cestas-atendente

Atendimento automatizado por WhatsApp + IA (Claude) para clientes da
**Cestas Company** e **Flower Store**. Terceiro app irmao de
[cestas-company](https://github.com/dpedrosa-bit/cestas-company) (Flask) e
[cestas-routes](https://github.com/dpedrosa-bit/cestas-routes) (Node).

> Em uma sessao com a SDK Claude Code? Leia primeiro `CLAUDE.md`.

## Setup local

### 1. Pre-requisitos
- Python 3.11+
- Postgres acessivel (pode ser o mesmo do cestas-company)
- Conta Z-API em https://z-api.io (com 1 instancia ativa)
- Conta Anthropic Console em https://console.anthropic.com (com creditos)
- Token de acesso Shopify Admin (loja Cestas Company / Flower Store)

### 2. Variaveis de ambiente
Copie `.env.example` para `.env` e preencha:

```bash
cp .env.example .env
# edite .env
```

Variaveis obrigatorias:
- `ANTHROPIC_API_KEY`
- `ZAPI_INSTANCE_ID`, `ZAPI_INSTANCE_TOKEN`, `ZAPI_CLIENT_TOKEN`
- `SHOPIFY_DOMAIN`, `SHOPIFY_TOKEN`
- `DATABASE_URL`

### 3. Instalar dependencias
```bash
pip install -r requirements.txt
```

### 4. Subir local
```bash
python app.py
# servidor em http://localhost:5000
```

Verifica health:
```bash
curl http://localhost:5000/health
```

Esperado:
```json
{
  "status": "ok",
  "db": true,
  "anthropic_configured": true,
  "zapi_configured": true,
  "shopify_configured": true
}
```

### 5. Expor publicamente para Z-API (dev)
A Z-API precisa chegar no seu webhook. Use ngrok:
```bash
ngrok http 5000
# https://xxxx.ngrok.io/webhook/zapi
```
Configure essa URL como webhook na Z-API → Configuracoes → Webhook ao Receber.

## Deploy (Railway)

Mesmo padrao dos projetos irmaos:

```bash
python deploy.py --check    # valida sintaxe + tamanho
python deploy.py            # commita e da push origin master:main
```

**Importante:** Railway monitora a branch `main` no GitHub, mas commits vao
para `master` local. O `deploy.py` ja faz `git push origin master:main`.

Configurar todas as ENV vars no painel Railway antes do primeiro deploy.

URL de producao: `https://cestas-atendente-production.up.railway.app`
(quando o servico for criado).

## Como testar end-to-end

1. Sobe local com `python app.py`
2. Expoe via ngrok
3. Configura webhook na Z-API
4. Manda WhatsApp para o numero da instancia Z-API perguntando "qual o
   status do meu pedido?"
5. Acompanha logs no terminal — voce vera:
   - `[webhook] phone=... text='qual o status do meu pedido?'`
   - `[atendente] tool_call=buscar_pedido_por_telefone input={'phone': '...'}`
   - `[atendente] tool_done=buscar_pedido_por_telefone 350ms`
   - `[webhook] resposta enviada para 5511... (XX chars)`

## Estrutura

```
cestas-atendente/
├── app.py                    # Flask + webhook + healthcheck
├── models.py                 # SQLAlchemy (atendente_sessions/messages/handoff)
├── zapi_adapter.py           # Z-API: parse + send_text
├── anthropic_adapter.py      # Claude: tool use loop + caching
├── tools.py                  # Definicoes de tools + run_tool
├── shopify_client.py         # Shopify Admin REST helper
├── deploy.py                 # Validacao + push para Railway
├── Dockerfile + Procfile + nixpacks.toml + railway.toml
├── requirements.txt
├── .env.example
├── CLAUDE.md                 # Contexto para Claude Code (IA)
├── ARCHITECTURE.md           # Detalhes tecnicos
└── README.md                 # Este arquivo
```

## Custo estimado

Com prompt caching ativo (ja configurado):
- ~US$ 0.01 por conversa em Haiku 4.5
- Em 1.000 conversas/mes: ~US$ 10/mes em IA
- Z-API: ~R$ 99/mes por instancia

Ver `ARCHITECTURE.md` → Estimativa de custo para detalhes.

## Roadmap

- **Fase 1 (este MVP)** — read-only, 3 tools (buscar_pedido_por_telefone,
  buscar_pedido_por_numero, escalar_para_humano)
- **Fase 2** — tools que consultam cestas-routes (producao, entrega) +
  painel admin minimo
- **Fase 3** — handoff fluido com humano assumindo via painel
- **Fase 4** — tools de escrita (reagendar, atualizar endereco) com
  confirmacao explicita do cliente

Ver `ARCHITECTURE.md` → Pendencias para o plano completo.
