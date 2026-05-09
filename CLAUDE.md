# CLAUDE.md
# Guia de contexto para sessoes Claude Code
# Leia este arquivo PRIMEIRO em qualquer nova sessao

---

## O QUE E ESTE PROJETO

Atendimento automatizado por WhatsApp (via Z-API) usando Claude para clientes
da **Cestas Company** e **Flower Store**. Terceiro app irmao de:

- **cestas-company** (Flask) — admin de entrega + widget das duas lojas Shopify
- **cestas-routes** (Node) — paineis de roteirizacao e producao

Este app **consulta dados reais** dos pedidos (Shopify direto na Fase 1,
cestas-routes nas fases seguintes) e responde duvidas comuns dos clientes
no WhatsApp como um atendente humano faria olhando os paineis.

## LEIA ESTES ARQUIVOS PARA CONTEXTO COMPLETO:
1. `ARCHITECTURE.md` — fluxo, tabelas Postgres, tools, env vars
2. `README.md` — setup local + deploy

## ARQUIVOS PRINCIPAIS:
- `app.py` — Flask entrypoint + webhook /webhook/zapi + /health
- `models.py` — SQLAlchemy: AtendenteSession, AtendenteMessage, AtendenteHandoff
- `zapi_adapter.py` — parse webhook Z-API + envio de mensagens
- `anthropic_adapter.py` — tool use loop + prompt caching + persistencia
- `tools.py` — definicoes de tools + implementacoes
- `shopify_client.py` — cliente REST Admin API (busca por telefone, por numero)
- `deploy.py` — validacao + push para Railway (mesmo padrao dos outros projetos)

## URL DE PRODUCAO:
https://cestas-atendente-production.up.railway.app  *(quando criado no Railway)*

## REPOSITORIO:
https://github.com/dpedrosa-bit/cestas-atendente  *(a criar)*

## DEPLOY:
```bash
python deploy.py --check    # valida sintaxe e tamanho
python deploy.py            # commit + push origin master:main
```

Railway monitora a branch `main` no GitHub; commits vao para `master` local.

## REGRAS DE OURO (herdadas dos projetos irmaos):
1. **Nunca usar patches auto-correctivos** — sempre gerar arquivo completo
2. **Sempre verificar duplicatas** antes de fazer push
3. **flask-cors** resolve CORS — nunca adicionar handlers manuais
4. **O arquivo local pode divergir** do Railway — sempre `deploy.py --check`
5. **Snippets Shopify** sao independentes do backend (nao se aplica aqui — sem snippets)

## REGRAS ESPECIFICAS DO ATENDENTE:
6. **IA NUNCA inventa dados** — system prompt obriga consultar tool antes
   de afirmar status, data, mensagem. Nao remova essas instrucoes.
7. **Read-only nas tools** ate calibrar — sem `reagendar`, `cancelar`, etc.
   ate ter 1 mes de uso real validado.
8. **Prompt caching obrigatorio** — system prompt + tools com cache_control
   (ja configurado no anthropic_adapter). Verificar `usage.cache_read_*` em
   producao para confirmar hits.
9. **Mix de modelos** — Haiku 4.5 default (`MODEL_DEFAULT`), Sonnet 4.6
   apenas se `force_complex=True` no respond(). Opus 4.7 nao usado para nao
   estourar custo no MVP.
10. **Limite de turnos por sessao** — `MAX_TURNS_PER_SESSION` (default 20).
    Apos isso o atendente escala automaticamente para humano.

## PROXIMA GRANDE TAREFA (Fase 1 → Fase 2):
- Painel admin minimo em `/admin/sessions` para a equipe ver conversas em
  andamento e marcar handoff manual.
- Tools que consultam o cestas-routes:
  - `status_producao(order_id)` — via /api/production
  - `status_entrega(order_id)` — via /api/orders + lookup de rota
- Tool que consulta o cestas-company:
  - `slots_disponiveis(data, cep)` — via /api/delivery/config

## DECISOES IMPORTANTES JA TOMADAS:
- Stack: Python + Flask (mesmo padrao do cestas-company)
- Postgres: compartilhado com cestas-company, tabelas com prefixo `atendente_`
- Gateway WhatsApp: Z-API na Fase 1; Meta Cloud API depois de validado
- Shopify access: REST com SHOPIFY_TOKEN estatico (cestas-company usa OAuth
  Client Credentials Grant — pode migrar depois)
- System prompt com tom de voz e regras especificas: ADIADO para fase
  posterior — placeholder generico funciona para MVP

## VARIAVEIS DE AMBIENTE NO RAILWAY (todas obrigatorias para producao):
- `ANTHROPIC_API_KEY`
- `ZAPI_INSTANCE_ID`, `ZAPI_INSTANCE_TOKEN`, `ZAPI_CLIENT_TOKEN`
- `SHOPIFY_DOMAIN`, `SHOPIFY_TOKEN`
- `DATABASE_URL` (mesma string da instancia Postgres do cestas-company)
- `CESTAS_ROUTES_URL`, `CESTAS_COMPANY_URL` (para tools de fase 2)
- `PORT` (Railway preenche automaticamente)
