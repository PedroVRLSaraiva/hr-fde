# API do Adapter do TMS Legado

> API HTTP/JSON que o workflow do agente de voz chama. Ela esconde o protocolo TCP do TMS legado, as falhas injetadas (~16% das chamadas) e o `MAX_BUY` (`max_rate`), que aparece como `max_rate` em `GET /loads/search` e `GET /loads/{load_id}`.
>
> Código: `adapter/server.py` (HTTP) e `adapter/service.py` (regras). Protocolo do TMS: [`Integracao TMS Legado.md`](Integracao%20TMS%20Legado.md).
> Exemplos abaixo foram executados com `curl` contra o TMS real em 2026-10-20.

## Subir o servidor

```bash
python3 -m adapter.server        # http://127.0.0.1:8080 (variável PORT muda a porta)
```

Lê `TMS_HOST`, `TMS_PORT` e `TMS_TOKEN` do `.env` (ou do ambiente). Só stdlib do Python, sem dependências. O servidor escuta apenas em `127.0.0.1`.

## Autenticação

Toda rota, inclusive `/health`, exige o header:

```
Authorization: Bearer <TMS_TOKEN>
```

Sem o header ou com token errado: `401 {"error": "unauthorized"}`. O token nunca é escrito no log.

## Convenções

- Corpo e respostas em JSON. Datas em **UTC** com sufixo `Z` (o fuso real de `PICKUP_DT` ainda não foi confirmado com o TMS; assumido UTC).
- Valores monetários são inteiros em USD.
- Uma carga só é oferecida se `status == OPEN` e o pickup for no futuro. Uma carga reservada continua existindo no TMS (como `PENDING`), mas a API a trata como indisponível.
- Todo campo de texto vindo da voz é sanitizado (`|`, quebras de linha) antes de ir ao TMS.

## Endpoints

### `GET /loads/search`

Busca até 3 cargas reserváveis, ordenadas por data de pickup.

| Parâmetro | Obrigatório | Descrição |
|---|---|---|
| `origin_state` | sim, na prática | UF (`GA`) ou nome por extenso (`Georgia`) |
| `origin_city` | não | cidade de origem (aceita prefixo, como `Atla`) |
| `dest_state` | não | UF ou nome por extenso |
| `equipment` | não | `DRY_VAN`, `REEFER`, `FLATBED`, `STEP_DECK`, `POWER_ONLY`, ou linguagem natural (`dry van`, `van`, `refrigerado`, `step deck`, `power only`) |
| `pickup_date` | não | `YYYYMMDD` |

**Cascata:** se nada bate com todos os filtros, relaxa na ordem: tira a data, depois o destino, depois a cidade, até ficar só a origem. Quando isso acontece, a resposta traz `"relaxed": true`, e o agente deve avisar o carrier que as opções são parecidas, não exatas.

```bash
curl -H "Authorization: Bearer $TMS_TOKEN" \
  "http://127.0.0.1:8080/loads/search?origin_state=Georgia&equipment=flatbed"
```
```json
{"loads": [{"load_id": "LD01012",
  "origin": {"city": "Atlanta", "state": "GA", "zip": "30303"},
  "destination": {"city": "Jacksonville", "state": "FL", "zip": "32202"},
  "pickup_datetime": "2026-10-26T13:03:00Z", "delivery_datetime": "2026-10-28T15:03:00Z",
  "equipment_type": "FLATBED", "loadboard_rate": 755, "weight": 26713,
  "commodity_type": "Machinery", "num_of_pieces": 6, "miles": 285,
  "dimensions": "49ft x 8ft x 9ft", "notes": null, "status": "OPEN", "max_rate": 891}],
 "relaxed": false}
```

**Campos de cada carga** (a mesma estrutura em `/loads/search` e `/loads/{load_id}`; inclui `max_rate`):

| Campo | Tipo | Descrição |
|---|---|---|
| `load_id` | string | id da carga |
| `origin`, `destination` | objeto | `city`, `state` (UF), `zip` (pode ser `null`) |
| `pickup_datetime`, `delivery_datetime` | string ISO 8601 UTC | `delivery_datetime` pode ser `null` |
| `equipment_type` | string | `DRY_VAN`, `REEFER`, `FLATBED`, `STEP_DECK` ou `POWER_ONLY` |
| `loadboard_rate` | inteiro | valor anunciado, em USD |
| `weight` | inteiro | peso (lb) |
| `commodity_type` | string | tipo de mercadoria |
| `num_of_pieces` | inteiro | número de peças |
| `miles` | inteiro | distância |
| `dimensions` | string | dimensões |
| `notes` | string ou `null` | observações |
| `status` | string | sempre `OPEN` nas respostas desta API |
| `max_rate` | inteiro | teto que podemos pagar (`MAX_BUY`), em USD. **Uso interno:** o agente grava no fluxo e nunca revela ao carrier |

**Exemplo com cascata** (pediu `dest_state=TX`, não havia; relaxou o destino):
```json
{"loads": [
  {"load_id": "LD01012",
   "origin": {"city": "Atlanta", "state": "GA", "zip": "30303"},
   "destination": {"city": "Jacksonville", "state": "FL", "zip": "32202"},
   "pickup_datetime": "2026-10-26T13:03:00Z", "delivery_datetime": "2026-10-28T15:03:00Z",
   "equipment_type": "FLATBED", "loadboard_rate": 755, "weight": 26713,
   "commodity_type": "Machinery", "num_of_pieces": 6, "miles": 285,
   "dimensions": "49ft x 8ft x 9ft", "notes": null, "status": "OPEN", "max_rate": 891},
  {"load_id": "LD01044",
   "origin": {"city": "Atlanta", "state": "GA", "zip": "30303"},
   "destination": {"city": "Charlotte", "state": "NC", "zip": "28202"},
   "pickup_datetime": "2026-10-27T09:00:00Z", "delivery_datetime": null,
   "equipment_type": "FLATBED", "loadboard_rate": 640, "weight": 18200,
   "commodity_type": "Steel coils", "num_of_pieces": 3, "miles": 244,
   "dimensions": null, "notes": "Tarp required", "status": "OPEN", "max_rate": 720}
 ],
 "relaxed": true}
```

**Sem resultado:**
```json
{"loads": [], "relaxed": false}
```

**Estado ou equipamento desconhecido (`400`):**
```json
{"error": "bad_request", "detail": "equipamento desconhecido: 'caminhao'"}
```

### `GET /loads/{load_id}`

Detalhe de uma carga para o pitch. Mesmo formato de um item de `loads` acima.

| Status | Quando |
|---|---|
| 200 | carga reservável |
| 404 `load_unavailable` | carga existe mas está reservada ou com pickup vencido |
| 404 `not_found` | id inexistente |

Os campos são os da tabela acima, incluindo `max_rate`. Ofertas acima dele são barradas em `/negotiation/evaluate` e `/book`.

Exemplo `200`:
```json
{"load_id": "LD01012",
 "origin": {"city": "Atlanta", "state": "GA", "zip": "30303"},
 "destination": {"city": "Jacksonville", "state": "FL", "zip": "32202"},
 "pickup_datetime": "2026-10-26T13:03:00Z", "delivery_datetime": "2026-10-28T15:03:00Z",
 "equipment_type": "FLATBED", "loadboard_rate": 755, "weight": 26713,
 "commodity_type": "Machinery", "num_of_pieces": 6, "miles": 285,
 "dimensions": "49ft x 8ft x 9ft", "notes": null, "status": "OPEN", "max_rate": 891}
```
Exemplo `404`:
```json
{"error": "load_unavailable"}
```
```json
{"error": "not_found"}
```

### `POST /negotiation/evaluate`

Compara o valor pedido pelo carrier com o teto da carga (`MAX_BUY`, guardado no backend). `MAX_BUY` é a referência única: qualquer valor até ele é válido, sem negociar para baixo, porque a margem já está nele.

Corpo: `{"load_id": "LD01012", "ask": 700}` (`ask` inteiro positivo, em USD).

| Situação | Resposta |
|---|---|
| `ask ≤ MAX_BUY` | `{"decision": "accept", "rate": <ask>}` |
| `ask > MAX_BUY` | `{"decision": "counter", "rate": <MAX_BUY>}` |

```bash
curl -X POST -H "Authorization: Bearer $TMS_TOKEN" \
  -d '{"load_id":"LD01012","ask":5000}' http://127.0.0.1:8080/negotiation/evaluate
# {"decision": "counter", "rate": 891}
```

Exemplos de resposta (`200`):
```json
{"decision": "accept", "rate": 700}
```
```json
{"decision": "counter", "rate": 891}
```
`ask` inválido (`400`):
```json
{"error": "bad_request", "detail": "ask deve ser inteiro positivo"}
```

Atenção: o `counter` devolve o próprio teto, ou seja, informa ao carrier o máximo que pagamos. O agente deve usá-lo como **última oferta**. Nunca é devolvido valor acima do teto. `ask` inválido: `400`.

### `POST /loads/{load_id}/book`

Fecha a reserva com o valor que o carrier aceitou ao final da negociação.

Corpo:

| Campo | Obrigatório | Descrição |
|---|---|---|
| `mc_num` | sim | número MC do carrier (`MC123456` ou `123456`; só dígitos são enviados) |
| `agreed_rate` | sim | **inteiro** positivo em USD, o valor aceito pelo carrier |
| `call_id` | não | id da ligação, para rastreio |

Resposta (sempre `200`, o resultado vem em `result`):

| `result` | Significado | O que o agente diz |
|---|---|---|
| `booked` | reservada; traz `booking_ref` | confirma a reserva |
| `booked_unconfirmed` | uma tentativa ficou sem resposta e o retry mostrou que a carga já estava reservada por este token; provavelmente é nossa, mas sem `booking_ref` | "reservada, o representante vai confirmar os detalhes"; sinalizar para revisão humana |
| `unavailable` | carga já foi reservada por outra ligação, ou não está mais aberta | oferece outra carga |
| `error` + `reason` | ver abaixo | oferece callback |

Valores de `reason`: `rate_above_ceiling` (`agreed_rate` acima do `MAX_BUY`; **barrado antes de chegar ao TMS**), `needs_human` (duas tentativas sem resposta clara), `lock_timeout`, ou o código do TMS em minúsculas (por exemplo `invalid_rate`).

```bash
curl -X POST -H "Authorization: Bearer $TMS_TOKEN" \
  -d '{"mc_num":"123456","agreed_rate":5000}' http://127.0.0.1:8080/loads/LD01012/book
# {"result": "error", "reason": "rate_above_ceiling"}
```

Exemplos de resposta (todos `200`, exceto entrada inválida):
```json
{"result": "booked", "booking_ref": "LT9RTA3KPYMF2YCZ"}
```
```json
{"result": "booked_unconfirmed"}
```
```json
{"result": "unavailable"}
```
```json
{"result": "error", "reason": "rate_above_ceiling"}
```
```json
{"result": "error", "reason": "needs_human"}
```
```json
{"result": "error", "reason": "lock_timeout"}
```
```json
{"result": "error", "reason": "invalid_rate"}
```
`booking_ref` é opaco (16 caracteres): guardar como string, nunca parsear. Entrada inválida (`400`):
```json
{"error": "bad_request", "detail": "agreed_rate deve ser inteiro positivo"}
```

**Garantias**
- Uma reserva por carga de cada vez (lock por `LOAD_ID`): se duas ligações fecham a mesma carga, uma recebe `booked` e a outra `unavailable`.
- O `LOAD_BOOK` nunca é repetido às cegas: no máximo 2 tentativas, e a segunda só depois de uma falha sem resposta.
- **Irreversível:** o TMS não tem cancelamento, e um booking bem-sucedido consome a carga para sempre. O estado inicial devolvido pelo TMS é `PENDING` (tratado como reservado).
- Entrada inválida (`mc_num` vazio, `agreed_rate` ≤ 0 ou não inteiro): `400`.

### `GET /health`

```json
{"adapter": "ok", "tms": "ok", "breaker_open": false, "metrics": {"DEBUG_ECHO.ok": 1}}
```

Com o TMS fora do ar e o circuit breaker aberto:
```json
{"adapter": "ok", "tms": "error:timeout", "breaker_open": true, "metrics": {"DEBUG_ECHO.ok": 1, "LOAD_QUERY.fail": 6}}
```

`tms` usa `DEBUG_ECHO` (confirma conectividade e autenticação, **não** a saúde operacional, pois ele não passa pela injeção de falhas). `metrics` conta sucessos e falhas por comando.

## Erros

| HTTP | Corpo | Causa |
|---|---|---|
| 400 | `{"error": "bad_request", "detail": "..."}` | parâmetro ou corpo inválido |
| 401 | `{"error": "unauthorized"}` | token ausente ou errado |
| 404 | `{"error": "not_found"}` / `{"error": "load_unavailable"}` | rota ou carga inexistente / indisponível |
| 502 | `{"error": "<codigo_do_tms>"}` | erro definitivo do TMS não mapeado |
| 503 | `{"error": "TMS_UNAVAILABLE"}` | TMS fora do ar, ou circuit breaker aberto |

Exemplos de corpo:
```json
{"error": "bad_request", "detail": "mc_num inválido"}
```
```json
{"error": "unauthorized"}
```
```json
{"error": "not_found"}
```
```json
{"error": "invalid_rate"}
```
```json
{"error": "TMS_UNAVAILABLE"}
```
(`400`, `401`, `404`, `502`, `503`, nesta ordem.)

## Resiliência

- Cada comando abre uma conexão TCP nova; leitura com timeout de 5 s (8 s no `LOAD_BOOK`).
- `LOAD_QUERY` e `LOAD_GET`: até 3 tentativas com espera crescente e aleatória. Com 16% de falha por chamada, a chance de falhar nas 3 é de cerca de 0,4%.
- Cache de 45 s para buscas e detalhes; invalidado após qualquer reserva.
- Circuit breaker: 6 falhas seguidas abrem por 20 s e a API responde `503` na hora, para o agente oferecer um callback em vez de deixar o carrier esperando.

## Limites conhecidos

- O lock e o estado das reservas ficam **em memória**: valem para uma única instância e se perdem se o processo reiniciar (a ser trocado pelo Twin).
- O piso de `agreed_rate` aceito pelo TMS é desconhecido; um valor rejeitado volta como `error` com `reason: "invalid_rate"`.
- Fuso de `PICKUP_DT` assumido UTC; `MAX_RESULTS` máximo do TMS não determinado (a API pede no máximo 20 e devolve 3).
- A carga `LD01031` foi reservada durante os testes e não pode ser usada na demo.

## Testes

```bash
python3 -I tests/test_tms_client.py   # cliente TCP, servidor falso
python3 -I tests/test_service.py      # serviço e HTTP, sem rede
python3 -I tests/test_tms_live.py     # só leitura contra o TMS real (exige .env)
```
