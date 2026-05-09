# Cestas Atendente — Arquitetura

## Visao geral

```
WhatsApp ──► Z-API webhook ──► Flask /webhook/zapi
                                     │
                                     ├─► Postgres (sessoes + historico)
                                     │
                                     └─► Claude API (tool use)
                                              │
                                              ├─► tool: buscar_pedido_por_telefone
                                              │     └─► Shopify Admin REST
                                              │
                                              ├─► tool: buscar_pedido_por_numero
                                              │     └─► Shopify Admin REST
                                              │
                                              └─► tool: escalar_para_humano
                                                    └─► Postgres (handoff)
```

## Fluxo de uma mensagem

1. Cliente manda WhatsApp para o numero da loja
2. Z-API encaminha para `POST /webhook/zapi` (JSON com phone + text)
3. `zapi_adapter.parse_webhook` filtra: ignora grupos, eco, status, midias
4. `_get_or_create_session(phone)` busca sessao ativa ou cria nova
5. `anthropic_adapter.respond(session, text)`:
   - Persiste a msg do user em `atendente_messages`
   - Reconstroi historico completo (incluindo tool_use + tool_result blocks)
   - Chama Claude com system + tools + messages (loop ate end_turn)
   - Para cada tool_use: executa tool, persiste resultado, devolve para Claude
   - Persiste a resposta final do assistente
6. `zapi_adapter.send_text(phone, reply)` devolve a resposta via Z-API
7. Cliente recebe no WhatsApp

## Tabelas Postgres (prefixo `atendente_`)

### atendente_sessions
Uma sessao por numero de telefone. Reusa enquanto `status='active'`.
Ao escalar, status vira `handoff` e proxima mensagem cria nova sessao.

| Coluna | Tipo | Notas |
|---|---|---|
| id | int PK | |
| phone | varchar(32) | E.164 sem '+' (5511999999999) |
| status | varchar(16) | active / handoff / closed / expired |
| customer_id | varchar(64) | Shopify customer id (preenchido apos primeiro lookup) |
| customer_name | varchar(255) | Nome do contato no WhatsApp |
| meta | jsonb | Livre |
| turn_count | int | Contador de mensagens user |
| tokens_input_total / tokens_output_total | int | Agregados Anthropic |
| cache_read_total / cache_write_total | int | Agregados de prompt caching |
| created_at / last_message_at | timestamp | |

### atendente_messages
Log completo. Cada turno (user / assistant / tool) eh uma linha.

| Coluna | Tipo | Notas |
|---|---|---|
| id | int PK | |
| session_id | int FK | |
| role | varchar(16) | user / assistant / tool |
| content | text | Texto direto (user/assistant) ou JSON do tool result |
| tool_calls | jsonb | Para assistant: lista de {id,name,input}. Para tool: {tool_use_id, name} |
| model | varchar(64) | Modelo usado (apenas em assistant) |
| tokens_input / tokens_output | int | Da resposta especifica |
| cache_read / cache_write | int | Do mesmo turno |
| external_id | varchar(128) | message_id da Z-API (idempotencia) |

### atendente_handoff
Fila de escalacoes pendentes para a equipe humana resolver.

| Coluna | Tipo | Notas |
|---|---|---|
| id | int PK | |
| session_id | int FK | |
| reason | varchar(255) | Motivo curto |
| summary | text | Resumo da IA do que aconteceu ate aqui |
| status | varchar(16) | pending / taken / resolved |
| assigned_to | varchar(128) | Quem assumiu |

## Tools disponiveis (Fase 1)

### `buscar_pedido_por_telefone(phone)`
Usa o telefone do WhatsApp para achar o cliente na Shopify e retorna ate 5
pedidos mais recentes. Cobre o caso "qual o status do meu pedido?" sem
exigir numero.

**Implementacao:** `shopify_client.search_customers_by_phone()` →
`shopify_client.list_customer_orders()` → `summarize_order()`.

**Variantes de telefone:** `zapi_adapter.phone_variants()` gera 5+ formatos
(com/sem +55, com/sem 9, etc) para maximizar matching com o que esta na
Shopify.

### `buscar_pedido_por_numero(order_number)`
Aceita "CC12345", "#12345" ou "12345". Usa
`/admin/api/.../orders.json?name=#XXXXX&status=any`.

### `escalar_para_humano(motivo, resumo)`
Marca a sessao como `handoff` e cria registro em `atendente_handoff`.
Notificacao real para a equipe (email/Telegram) fica para Fase 3.

## Prompt caching

Render order da API: `tools` → `system` → `messages`.

O `_build_system_blocks()` coloca `cache_control: ephemeral` no ultimo
bloco do system, cacheando **tools + system juntos**. Isso garante:

- Primeiro turno da sessao: cache_creation (~1.25x preco normal)
- Demais turnos da mesma conversa: cache_read (~0.1x preco normal)

TTL default: 5min. Cobre conversas em andamento. Para alta concorrencia
fora desse horario, considerar `ttl: 1h` (mas custo de write 2x).

**Como verificar se esta funcionando:** apos algumas conversas em prod,
rodar query:

```sql
SELECT
  AVG(cache_read_total::float / NULLIF(tokens_input_total, 0)) as cache_hit_ratio,
  COUNT(*) as sessions
FROM atendente_sessions
WHERE created_at > NOW() - INTERVAL '24 hours'
  AND turn_count > 1;
```

Esperado: > 0.7 (70% dos input tokens vem de cache em conversas
multi-turno). Se < 0.3, ha invalidador silencioso (data/hora no system,
JSON nao deterministico, etc).

## Tool use loop manual

Usamos manual loop (nao tool runner) porque queremos:
- Logging de cada tool call com latencia
- Persistir tool_use + tool_result em Postgres no momento certo
- Side effects (escalacao muda session.status)
- Limite de iteracoes (`MAX_TOOL_LOOPS = 6`) — se a IA ficar em loop,
  paramos e mandamos mensagem de fallback

Stop conditions:
- `response.stop_reason == 'end_turn'`: resposta final, devolve texto
- `MAX_TOOL_LOOPS` atingido: fallback "vou chamar atendente humano"
- `RateLimitError`: mensagem amigavel pedindo para tentar de novo
- Outras excecoes: log + escalacao silenciosa

## Anti-loop e custos

- `MAX_TURNS_PER_SESSION = 20`: depois disso, qualquer mensagem nova vira
  handoff automatico. Protege contra cliente perdido em loop com IA.
- `MAX_TOOL_LOOPS = 6`: dentro de UM turno, no maximo 6 round-trips com
  tool. Cobre caso "buscar pedido → buscar mais detalhes → status entrega".
- `MAX_TOKENS_OUTPUT = 1024`: WhatsApp eh formato curto. Resposta maior
  trunca, mas raramente acontece.

## Estimativa de custo (com prompt caching)

Premissas:
- System prompt + tools: ~1.5k tokens
- Conversa media: 4 turnos
- Tool calls medio: 1 por turno
- Tool result medio: 800 tokens
- Output medio: 200 tokens

Por conversa em **Haiku 4.5** ($1/Mtoken input, $5/Mtoken output):
- Input cacheado (3 turnos): 3 × 1500 × 0.10 / 1M = ~$0.0005
- Input nao-cacheado (1 turno + tool results): ~5000 × 1.0 / 1M = ~$0.005
- Output (4 × 200): 800 × 5.0 / 1M = ~$0.004
- **Total: ~US$ 0.01 por conversa**

Em 1.000 conversas/mes: **~US$ 10/mes**. Em Sonnet 4.6 (3x mais caro):
~US$ 30/mes. Sustentavel para o volume previsto.

## Pendencias para fases seguintes

### Fase 2 — Tool use completo + painel admin
- [ ] Tool `status_producao(order_id)` consumindo `cestas-routes/api/production`
- [ ] Tool `status_entrega(order_id)` cruzando rotas + ETA do motorista
- [ ] Tool `slots_disponiveis(data, cep)` para "posso entregar tal dia?"
- [ ] Endpoint `GET /admin/sessions?status=active` para a equipe ver
- [ ] Endpoint `GET /admin/handoff?status=pending` fila de escalacao

### Fase 3 — Handoff fluido
- [ ] Endpoint `POST /admin/sessions/<id>/take` humano assume conversa
- [ ] Endpoint `POST /admin/sessions/<id>/say` humano envia msg via painel
- [ ] Notificacao push para equipe quando IA escala (Telegram bot ou email)
- [ ] Modo supervisao: humano valida resposta da IA antes de enviar

### Fase 4 — Acoes com confirmacao
- [ ] Tool `reagendar_entrega(order_id, nova_data)` com confirmacao explicita
- [ ] Tool `atualizar_endereco(order_id, novo_endereco)` se ainda nao despachado
- [ ] Tool `cancelar_pedido(order_id, motivo)` (sempre escala antes)

### Pendencias gerais
- [ ] Migrar Shopify access para Client Credentials Grant (igual cestas-company)
- [ ] Suporte a multi-loja (Cestas Company E Flower Store) — hoje so um SHOPIFY_DOMAIN
- [ ] Idempotencia por external_id (evita responder duas vezes se Z-API retentar)
- [ ] Webhook signing/auth (Z-API tem validacao opcional)
