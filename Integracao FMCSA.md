# Integração com a FMCSA (QCMobile API)

> Como o sistema vai usar a API pública da FMCSA para verificar a autoridade dos carriers (passo 2 do processo) e obter o telefone registrado para o OTP.
>
> **Fonte:** FMCSA Mobile Developer Website, `https://mobile.fmcsa.dot.gov/QCDevsite/`, páginas Home, Quick Start Guide, API Access, QCMobile API, API Response (Elements), Contact Us e Login.gov info, consultadas em 2026-10-13.
>
> A documentação oficial é **bem enxuta**: lista os endpoints e parte dos elementos, mas não traz o schema completo das respostas, os limites de uso nem um ambiente de sandbox. Os pontos marcados com **⚠️ A validar** vêm de conhecimento prévio da API ou de inferência e precisam ser confirmados com chamadas reais (seção 9).

---

## 1. Resumo para quem vai desenhar os processos

| Tema | Implicação |
|---|---|
| **API REST pública, JSON** | Integração simples por HTTPS. Não precisa de adapter complexo como o do TMS. |
| **Autenticação por `webKey` na query string** | A chave fica na URL, então **mascarar as URLs nos logs**. Ela é obtida com conta Login.gov. |
| **O carrier informa o MC, não o DOT** | Fluxo em 2 passos: `MC → /carriers/docket-number/{mc}` para obter o `dotNumber` e os dados básicos; depois, se necessário, `/carriers/{dot}/authority` para o detalhe da autoridade. |
| **Regra de aprovação é nossa** | A API devolve dados, não um "aprovado/reprovado". Definimos a regra: `allowToOperate = Y`, sem out-of-service e com autoridade de **carrier** ativa. |
| **`telephone` é o destino do OTP** | É o "registered phone number" pedido pelo compliance. Nunca usar um número falado na ligação. |
| **"Elements only appear if they have value"** | Campo ausente ≠ campo falso. O parser tem que tratar a ausência explicitamente e decidir de forma conservadora (*fail closed*). |
| **Sem SLA nem rate limit documentados** | Precisamos de timeout, retry, cache e um fallback de "não consegui verificar" que **não aprova** o carrier. |

---

## 2. Acesso e autenticação

### 2.1 Como obter a WebKey
1. Criar ou entrar numa conta **Login.gov** (email único, senha e telefone para o código de segurança). As contas antigas do Developer Site foram resetadas e só o Login.gov funciona.
2. Entrar no Developer Site → **My WebKeys** → **Get a new WebKey**.
3. Preencher o formulário:
   - **Application Name** (por exemplo, "Crystal Lane Carrier Verification")
   - **Type of Application**: *commercial* (é o caso de uma brokerage)
   - **How many users**: estimativa de usuários da chave
   - **Application Description**
   - **Client Secret**: palavra-semente para gerar a chave, única por conta. **Não é enviada nas chamadas e nunca deve ser compartilhada**, nem com o suporte.
4. A WebKey aparece na tela.

### 2.2 Como autenticar as chamadas
- Toda chamada leva `?webKey=<chave>` na query string.
- Chave inválida → JSON de *Authentication Failure* / HTTP 401.
- **Cuidados:**
  - A chave na URL acaba em logs de proxy, de APM e de erro. **Redigir `webKey=` em todos os logs.**
  - A chave fica em variável de ambiente ou no secret manager, nunca no repositório.
  - Uma chave por ambiente (dev/prod) facilita a revogação.

### 2.3 Suporte
- Email: **fmctechsup@dot.gov**. Incluir o email da conta Login.gov e **nunca** o client secret ou senhas.

---

## 3. Endpoints

Base URL: `https://mobile.fmcsa.dot.gov/qc/services/`

| Endpoint | Parâmetros | Uso no nosso fluxo |
|---|---|---|
| `GET /carriers/docket-number/{docketNumber}` | — | **Principal.** Busca pelo **MC number** informado pelo carrier |
| `GET /carriers/{dotNumber}` | — | Busca por USDOT (fallback se o carrier informar o DOT) |
| `GET /carriers/{dotNumber}/authority` | — | **Detalhe da autoridade de operação** (common/contract/broker) |
| `GET /carriers/{dotNumber}/oos` | — | Detalhe de out-of-service |
| `GET /carriers/{dotNumber}/docket-numbers` | — | Lista os dockets (MC/MX/FF) do DOT, para confirmar que o MC pertence ao DOT |
| `GET /carriers/{dotNumber}/basics` | — | Indicadores de segurança (BASICs). Opcional, como sinal de risco |
| `GET /carriers/{dotNumber}/cargo-carried` | — | Tipos de carga que o carrier declara. Opcional, para match com a commodity |
| `GET /carriers/{dotNumber}/operation-classification` | — | Classificação (por exemplo, Authorized For Hire). Opcional |
| `GET /carriers/name/{name}` | `start`, `size` | Busca por nome legal ou DBA. **Não usar para verificação**, porque é ambígua |

**Outras regras:**
- Todas as respostas são **JSON**.
- Respostas com vários carriers vêm **limitadas aos primeiros 50**; a paginação usa `start` e `size`.
- Exemplo do Quick Start: `GET /carriers/name/greyhound?webKey=...`

**Erros documentados:** `401 Unauthorized`, `400 Bad request`, `404 Not found`, `500 Internal Server Error`, além de *General Application Exception* e *Unknown Resource Exception*.

---

## 4. Elementos de resposta (documentados)

### 4.1 Carrier

| Elemento | Descrição | Valores | Uso |
|---|---|---|---|
| `allowToOperate` | o carrier pode operar por lei | `Y` / `N` | **critério de aprovação** |
| `outOfService` | recebeu ordem de out-of-service | `Y` / `N` | **critério de reprovação** |
| `outOfServiceDate` | data da ordem de OOS | `MM/DD/YYYY` | log/auditoria |
| `complaintCount` | reclamações recebidas pela FMCSA | número | sinal de risco (App) |
| `dotNumber` | número USDOT | número | chave para os sub-endpoints |
| `mcNumber` | número MC | número | conferir com o MC informado |
| `legalName` | razão social | string | confirmar com o carrier ("falo com a ACME Trucking?") |
| `dbaName` | nome fantasia | string | idem |
| `phyStreet`, `phyCity`, `phyState`, `phyZip`, `phyCountry` | endereço físico | string | auditoria |
| `telephone` | **telefone registrado** | string | **destino do OTP** |
| `busVehicle`, `limoVehicle`, `miniBusVehicle`, `motorCoachVehicle`, `vanVehicle`, `passengerVehicle` | contagem de veículos de passageiros | número | irrelevante (cargas) |

### 4.2 BASICs (`/basics`)

| Elemento | Descrição |
|---|---|
| `basicShortDesc`, `basicDesc`, `basicId` | identificação do BASIC |
| `percentile` | percentil de desempenho (ou `inconclusive`, `no violations`, `insufficient data`) |
| `rdDeficient` | violou o limite de desempenho on-road (`Y`/`N`) |
| `rdsvDeficient` | excedeu o limite de intervenção da FMCSA (`Y`/`N`) |
| `svDeficient` | violação grave em investigação nos últimos 12 meses (`Y`/`N`) |
| `snapShotDate` | última atualização (`MM/DD/YYYY`) |
| `totalInspectionWithViolation`, `totalViolation` | inspeções e violações em 24 meses |

BASICs reportados: Unsafe Driving, Driver Fitness, Fatigued Driving (HOS), Controlled Substance/Alcohol e Vehicle Maintenance.

### 4.3 Notas oficiais importantes
1. **"Elements only appear if they have value"**: os campos ausentes precisam de tratamento.
2. **Alguns elementos são mutuamente exclusivos.** Por exemplo, com `allowToOperate = Y` não há `outOfServiceDate`.

---

## 5. Pontos não documentados (⚠️ A validar)

A página de elementos lista só uma parte do que a API devolve. Pelo comportamento conhecido da QCMobile, espera-se o seguinte, **a confirmar com chamadas reais**:

| Item | Expectativa | Por que importa |
|---|---|---|
| Envelope | `{"content": ..., "retrievalDate": ...}`. No `docket-number`, `content` é uma **lista** de `{"carrier": {...}}`; no `/carriers/{dot}`, é um objeto `{"carrier": {...}}`. Sem resultado: `content` nulo ou vazio, **não necessariamente 404** | o parser precisa tratar lista, objeto e vazio |
| Nome real do campo | a doc diz `allowToOperate`, mas a API costuma devolver **`allowedToOperate`** | aceitar os dois nomes |
| Status de autoridade no objeto do carrier | `commonAuthorityStatus`, `contractAuthorityStatus`, `brokerAuthorityStatus` com valores `A` (ativa), `I` (inativa) e `N` (nenhuma) | é o que define **autoridade de carrier ativa** |
| Status do registro | `statusCode` (`A` = ativo) | critério adicional |
| Seguro | `bipdInsuranceOnFile`, `bipdInsuranceRequired`, `bipdRequiredAmount`, `cargoInsuranceOnFile` (em milhares de USD) | uma regra opcional de "seguro mínimo em dia" |
| Data de OOS | o campo pode vir como `oosDate` | aceitar os aliases |
| `telephone` | **confirmar se vem no endpoint de docket**; se não vier, buscar em `/carriers/{dot}` | é o requisito duro do OTP |
| Formato do `docketNumber` | só os dígitos do MC (`/docket-number/123456`), sem o prefixo `MC` | normalizar a entrada |
| Rate limit e latência | não documentados | dimensionar cache, timeout e retry |
| Tipos | números podem vir como string ou como número; datas em formatos variados | coerção tolerante |

---

## 6. Fluxo de verificação proposto

```
Carrier diz o MC ("MC 1-2-3-4-5-6" / "123456")
        │
        ▼
[1] Normalizar: só dígitos, sem prefixo MC, 1–8 dígitos  ──inválido──► pedir de novo (máx. 2x)
        │
        ▼
[2] GET /carriers/docket-number/{mc}
        ├─ vazio / 404 ─────────────────────────────► REPROVADO: mc_not_found
        ├─ 5xx / timeout (após retry) ──────────────► NÃO VERIFICADO: fmcsa_unavailable (não aprova)
        └─ 1 ou mais carriers
                │  (se vier mais de um, escolher o de mcNumber == mc; ambíguo → NÃO VERIFICADO)
                ▼
[3] Aplicar as regras de elegibilidade (seção 7)  ──falha──► REPROVADO: motivo
                │
                ▼
[4] (opcional) GET /carriers/{dot}/authority  → confirmar a autoridade de property ativa
                │
                ▼
[5] Obter o telephone (do objeto do carrier ou de /carriers/{dot})
        ├─ ausente ou inválido ──────────────────────► NÃO VERIFICADO: no_registered_phone → humano
        └─ ok → normalizar para E.164 (+1XXXXXXXXXX)
                │
                ▼
[6] Confirmar o nome com o carrier ("Estou falando com a <legalName>?")
                │
                ▼
APROVADO → segue para o OTP (enviado ao telephone do FMCSA)
```

**O que o agente recebe:** só `{status, carrier_name, reason_code}`. O agente **nunca recebe o telefone completo**: no máximo os 4 últimos dígitos, para algo como "mandei um código para o número terminado em 4321".

---

## 7. Regras de elegibilidade (propostas, a validar com o cliente)

| # | Regra | Fonte | Severidade |
|---|---|---|---|
| R1 | `allowToOperate`/`allowedToOperate == "Y"` | doc | **bloqueia** |
| R2 | `outOfService != "Y"` e sem `outOfServiceDate`/`oosDate` | doc | **bloqueia** |
| R3 | autoridade de **carrier** ativa: `commonAuthorityStatus == "A"` **ou** `contractAuthorityStatus == "A"` | ⚠️ inferido | **bloqueia** |
| R4 | **Só autoridade de broker não basta:** `brokerAuthorityStatus == "A"` sem R3 → reprovado (é um broker, não um carrier) | ⚠️ inferido | **bloqueia** |
| R5 | `statusCode == "A"` | ⚠️ inferido | bloqueia |
| R6 | o `mcNumber` retornado bate com o MC informado | — | bloqueia |
| R7 | seguro BIPD em dia (`bipdInsuranceOnFile ≥ bipdRequiredAmount`) | ⚠️ inferido | opcional (política) |
| R8 | BASICs com `rdsvDeficient == "Y"` ou `svDeficient == "Y"` | doc | **sinaliza** no App (não bloqueia no PoC) |
| R9 | `complaintCount` alto | doc | sinaliza |

**Princípio fail-closed:** se um campo de bloqueio estiver **ausente** (lembrando que "elements only appear if they have value") ou a API estiver fora, o carrier **não é aprovado** automaticamente. O agente encerra com educação e oferece retorno, e o caso vai para a fila humana no App.

**Códigos de motivo** (gravados no Twin e exibidos no App):
`mc_invalid_format`, `mc_not_found`, `not_allowed_to_operate`, `out_of_service`, `no_active_carrier_authority`, `broker_only_authority`, `inactive_registration`, `mc_mismatch`, `no_registered_phone`, `fmcsa_unavailable`, `ambiguous_result`.

O que o carrier ouve é genérico ("não consegui validar sua autoridade no FMCSA, um dos nossos reps vai entrar em contato"). **Não detalhar o motivo** ao telefone, para evitar que a regra seja contornada.

---

## 8. Resiliência, cache e segurança

- **Timeout:** ~3 s por chamada; **retry** 2x com backoff só em 5xx e timeout (nunca em 4xx).
- **Cache** por MC/DOT:
  - aprovado: TTL de ~24 h (a autoridade muda pouco)
  - reprovado: TTL curto (~1 h), porque o carrier pode estar regularizando
  - invalidação manual pelo App
- **Circuit breaker:** com a FMCSA fora, o resultado é `fmcsa_unavailable` (fail-closed), alerta no App e KPI de indisponibilidade.
- **Logs:** mascarar `webKey`; guardar só o telefone mascarado no Twin (`***-***-4321`) e o telefone completo apenas no serviço de OTP, de forma efêmera.
- **Auditoria:** gravar no Twin o `retrievalDate`, os campos usados na decisão e a regra que reprovou, o que dá a trilha de auditoria pedida no case.
- **Anti-social engineering:** o telefone do OTP **vem só da FMCSA**. Pedidos como "mudei de número", "manda para este outro" ou "meu dispatcher atende" → recusar e encaminhar a um humano.

---

## 9. Validações a fazer com a WebKey

1. `GET /carriers/docket-number/{mc}` com MCs reais conhecidos (ativo, inativo, OOS, só broker) e salvar as respostas como fixtures.
2. Confirmar o **envelope** (`content` como lista ou objeto) e o nome real dos campos (`allowedToOperate` ou `allowToOperate`, `oosDate` ou `outOfServiceDate`).
3. Confirmar se o **`telephone` vem no docket-number** ou só no `/carriers/{dot}`, e em que formato (`(555) 123-4567`?).
4. Conferir os campos de autoridade no objeto do carrier e no `/authority`.
5. Resposta para MC inexistente: 404 ou `content: []`?
6. Comportamento com MC que tem prefixo, zeros à esquerda e letras.
7. Latência p50/p95 e algum sinal de rate limit (429?) com ~50 chamadas espaçadas.
8. Montar uma **lista de MCs de teste** para os cenários do QA: aprovado, OOS, sem autoridade, broker-only e inexistente. Para a demo, como o telefone real do FMCSA não é nosso, usar uma *allowlist* de MCs de teste com override do telefone, desligada em produção.

---

## 10. Interface interna proposta

`POST /carriers/verify` (Integration API, chamada pelo workflow)

Requisição:
```json
{ "call_id": "c_123", "mc_number": "123456" }
```
Resposta ao agente:
```json
{ "status": "approved", "carrier_name": "ACME TRUCKING LLC", "phone_last4": "4321", "reason_code": null }
```
```json
{ "status": "rejected", "carrier_name": null, "phone_last4": null, "reason_code": "out_of_service" }
```
Internamente, o resultado completo (dotNumber, campos da decisão, telefone em E.164 e `retrievalDate`) vai para o Twin e para o serviço de OTP.

---

## 11. Perguntas em aberto para o cliente

1. Seguro mínimo (BIPD/cargo) deve bloquear ou só sinalizar?
2. BASICs com alerta (`rdsvDeficient`/`svDeficient`) bloqueiam?
3. Exige um tempo mínimo de autoridade ativa (por exemplo, mais de 90 dias), uma regra comum contra fraude?
4. O que fazer quando o telefone no FMCSA está desatualizado: só um humano, ou outro canal verificado (por exemplo, o email do FMCSA)?
5. O carrier pode se identificar por USDOT em vez de MC?

---

## 12. Exemplos reais de chamada (MC 193047)

> Chamadas reais feitas em **2026-10-20** com o MC **193047** (carrier: **AIR EXPRESS INTERNATIONAL USA INC**, DBA DHL GLOBAL FORWARDING, USDOT **74167**). Base URL: `https://mobile.fmcsa.dot.gov/qc/services`. A `webKey` foi substituída por `<WEBKEY>`; o restante é a resposta **integral**, sem edição.

### 12.0 Resumo do que a chamada real mostrou

| Item | Observado | Impacto |
|---|---|---|
| Campo de operação | `allowedToOperate` (com **d**), não `allowToOperate` | o parser deve aceitar os dois nomes (§5) |
| Envelope | `{content, retrievalDate}`. `content` é **lista** em `docket-number`, `authority`, `oos`, `docket-numbers`, `basics`, `cargo-carried`, `operation-classification` e `name`; é **objeto** em `/carriers/{dot}` | confirma a expectativa da §5 |
| **`telephone`** | **ausente** em `docket-number`, `/carriers/{dot}` e `name`. Também não há nenhum campo com `phone` ou `fax` | **bloqueia o desenho atual do OTP** (§6, passo 5): a QCMobile não entrega o telefone registrado. Ver a decisão em aberto abaixo |
| Autoridade no carrier | `commonAuthorityStatus: A`, `contractAuthorityStatus: A`, `brokerAuthorityStatus: A` | este carrier é carrier **e** broker; R3 passa |
| Autoridade no `/authority` | `contractAuthorityStatus: N` (diverge do objeto do carrier) | as duas fontes podem divergir; definir qual manda |
| `oosDate` | `null` (campo presente, sem valor) | tratar `null` como ausente |
| Tipos | números como string (`"5000"`) e como número (`181`); `bipdInsuranceRequired: "u"` | coerção tolerante |
| Endereço | `phyZipcode` (não `phyZip`) | ajustar a tabela da §4.1 |
| MC no carrier | o objeto `carrier` **não traz `mcNumber`**; o MC só aparece em `/authority` e `/docket-numbers` (`docketNumber` + `prefix`) | a regra R6 (conferir o MC) exige chamar `/docket-numbers` ou `/authority` |
| Latência | 400–810 ms (`name`: 2,2 s) | timeout de 3 s da §8 é apertado para `name`, ok para os demais |

**Decisão em aberto (nova):** como a API não devolve telefone, de onde vem o número para o OTP? Opções: (a) consulta ao SAFER/L&I ou a dataset de census da FMCSA (a validar), (b) outro canal verificado, como o email do registro, (c) fila humana. A investigação está na **seção 13**: a fonte recomendada é o dataset de census no data.transportation.gov.

### 12.1 Busca por MC (principal)

**Endpoint:** `GET /carriers/docket-number/193047`  
**URL chamada:** `https://mobile.fmcsa.dot.gov/qc/services/carriers/docket-number/193047?webKey=<WEBKEY>`  
**Status:** 200 · **Latência:** 810 ms

> `content` é uma **lista** de `{_links, carrier}`. O `carrier` traz autoridade, seguro, inspeções e crashes; **não traz `telephone`**. Não há `retrievalDate` dentro do carrier, só no envelope.

**Resposta completa:**

```json
{
  "content": [
    {
      "_links": {
        "basics": {
          "href": "https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/basics"
        },
        "cargo carried": {
          "href": "https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/cargo-carried"
        },
        "operation classification": {
          "href": "https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/operation-classification"
        },
        "docket numbers": {
          "href": "https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/docket-numbers"
        },
        "carrier active-For-hire authority": {
          "href": "https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/authority"
        }
      },
      "carrier": {
        "allowedToOperate": "Y",
        "bipdInsuranceOnFile": "5000",
        "bipdInsuranceRequired": "u",
        "bipdRequiredAmount": "750",
        "bondInsuranceOnFile": "75",
        "bondInsuranceRequired": "Y",
        "brokerAuthorityStatus": "A",
        "cargoInsuranceOnFile": "0",
        "cargoInsuranceRequired": "u",
        "carrierOperation": {
          "carrierOperationCode": "A",
          "carrierOperationDesc": "Interstate"
        },
        "censusTypeId": {
          "censusType": "C",
          "censusTypeDesc": "CARRIER",
          "censusTypeId": 1
        },
        "commonAuthorityStatus": "A",
        "contractAuthorityStatus": "A",
        "crashTotal": 0,
        "dbaName": "DHL GLOBAL FORWARDING",
        "dotNumber": 74167,
        "driverInsp": 7,
        "driverOosInsp": 0,
        "driverOosRate": 0,
        "driverOosRateNationalAverage": "5.51",
        "ein": 61113864,
        "fatalCrash": 0,
        "hazmatInsp": 0,
        "hazmatOosInsp": 0,
        "hazmatOosRate": 0,
        "hazmatOosRateNationalAverage": "4.5",
        "injCrash": 0,
        "isPassengerCarrier": null,
        "issScore": null,
        "legalName": "AIR EXPRESS INTERNATIONAL USA INC",
        "mcs150Outdated": "N",
        "oosDate": null,
        "oosRateNationalAverageYear": "2009-2010",
        "phyCity": "DARIEN",
        "phyCountry": "US",
        "phyState": "CT",
        "phyStreet": "120 TOKENEKE RD",
        "phyZipcode": "06820-4825",
        "reviewDate": "1987-03-20",
        "reviewType": "S",
        "safetyRating": "C",
        "safetyRatingDate": "1990-03-02",
        "safetyReviewDate": "1987-03-20",
        "safetyReviewType": "S",
        "snapshotDate": null,
        "statusCode": "A",
        "totalDrivers": 181,
        "totalPowerUnits": 102,
        "towawayCrash": 0,
        "vehicleInsp": 3,
        "vehicleOosInsp": 0,
        "vehicleOosRate": 0,
        "vehicleOosRateNationalAverage": "20.72"
      }
    }
  ],
  "retrievalDate": "2026-10-20T16:59:35.997+0000"
}
```

### 12.2 Busca por USDOT

**Endpoint:** `GET /carriers/74167`  
**URL chamada:** `https://mobile.fmcsa.dot.gov/qc/services/carriers/74167?webKey=<WEBKEY>`  
**Status:** 200 · **Latência:** 410 ms

> `content` é um **objeto** `{_links, carrier}` (não lista). Mesmo conteúdo do docket, com `_links.self` a mais e `isPassengerCarrier` preenchido (`"N"`; no docket veio `null`).

**Resposta completa:**

```json
{
  "content": {
    "_links": {
      "basics": {
        "href": "https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/basics"
      },
      "cargo carried": {
        "href": "https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/cargo-carried"
      },
      "operation classification": {
        "href": "https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/operation-classification"
      },
      "docket numbers": {
        "href": "https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/docket-numbers"
      },
      "carrier active-For-hire authority": {
        "href": "https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/authority"
      },
      "self": {
        "href": "https://mobile.fmcsa.dot.gov/qc/services/carriers/74167"
      }
    },
    "carrier": {
      "allowedToOperate": "Y",
      "bipdInsuranceOnFile": "5000",
      "bipdInsuranceRequired": "u",
      "bipdRequiredAmount": "750",
      "bondInsuranceOnFile": "75",
      "bondInsuranceRequired": "Y",
      "brokerAuthorityStatus": "A",
      "cargoInsuranceOnFile": "0",
      "cargoInsuranceRequired": "u",
      "carrierOperation": {
        "carrierOperationCode": "A",
        "carrierOperationDesc": "Interstate"
      },
      "censusTypeId": {
        "censusType": "C",
        "censusTypeDesc": "CARRIER",
        "censusTypeId": 1
      },
      "commonAuthorityStatus": "A",
      "contractAuthorityStatus": "A",
      "crashTotal": 0,
      "dbaName": "DHL GLOBAL FORWARDING",
      "dotNumber": 74167,
      "driverInsp": 7,
      "driverOosInsp": 0,
      "driverOosRate": 0,
      "driverOosRateNationalAverage": "5.51",
      "ein": 61113864,
      "fatalCrash": 0,
      "hazmatInsp": 0,
      "hazmatOosInsp": 0,
      "hazmatOosRate": 0,
      "hazmatOosRateNationalAverage": "4.5",
      "injCrash": 0,
      "isPassengerCarrier": "N",
      "issScore": null,
      "legalName": "AIR EXPRESS INTERNATIONAL USA INC",
      "mcs150Outdated": "N",
      "oosDate": null,
      "oosRateNationalAverageYear": "2009-2010",
      "phyCity": "DARIEN",
      "phyCountry": "US",
      "phyState": "CT",
      "phyStreet": "120 TOKENEKE RD",
      "phyZipcode": "06820-4825",
      "reviewDate": "1987-03-20",
      "reviewType": "S",
      "safetyRating": "C",
      "safetyRatingDate": "1990-03-02",
      "safetyReviewDate": "1987-03-20",
      "safetyReviewType": "S",
      "snapshotDate": null,
      "statusCode": "A",
      "totalDrivers": 181,
      "totalPowerUnits": 102,
      "towawayCrash": 0,
      "vehicleInsp": 3,
      "vehicleOosInsp": 0,
      "vehicleOosRate": 0,
      "vehicleOosRateNationalAverage": "20.72"
    }
  },
  "retrievalDate": "2026-10-20T16:59:36.482+0000"
}
```

### 12.3 Autoridade de operação

**Endpoint:** `GET /carriers/74167/authority`  
**URL chamada:** `https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/authority?webKey=<WEBKEY>`  
**Status:** 200 · **Latência:** 407 ms

> Lista de `carrierAuthority`. Aqui o `docketNumber` e o `prefix` (`MC`) aparecem, e `authorizedForProperty`/`authorizedForBroker` dizem o tipo de carga. **Atenção:** `contractAuthorityStatus` é `"N"` aqui, mas `"A"` no objeto do carrier.

**Resposta completa:**

```json
{
  "content": [
    {
      "carrierAuthority": {
        "applicantID": 154362,
        "authority": "N",
        "authorizedForBroker": "Y",
        "authorizedForHouseholdGoods": "N",
        "authorizedForPassenger": "N",
        "authorizedForProperty": "Y",
        "brokerAuthorityStatus": "A",
        "commonAuthorityStatus": "A",
        "contractAuthorityStatus": "N",
        "docketNumber": 193047,
        "dotNumber": 74167,
        "prefix": "MC"
      },
      "_links": {
        "self": {
          "href": "https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/authority/154362"
        }
      }
    }
  ],
  "retrievalDate": "2026-10-20T16:59:36.893+0000"
}
```

### 12.4 Out-of-service

**Endpoint:** `GET /carriers/74167/oos`  
**URL chamada:** `https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/oos?webKey=<WEBKEY>`  
**Status:** 200 · **Latência:** 528 ms

> `content: []` quando não há ordem de OOS. Vazio = sem OOS, não é erro.

**Resposta completa:**

```json
{
  "content": [],
  "retrievalDate": "2026-10-20T16:59:37.403+0000"
}
```

### 12.5 Dockets do DOT

**Endpoint:** `GET /carriers/74167/docket-numbers`  
**URL chamada:** `https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/docket-numbers?webKey=<WEBKEY>`  
**Status:** 200 · **Latência:** 498 ms

> Lista todos os dockets (`MC`, `FF`...). Serve para confirmar que o MC pertence ao DOT. O `_links.self` aponta para `/mc-numbers`.

**Resposta completa:**

```json
{
  "content": [
    {
      "docketNumber": 193047,
      "docketNumberId": 207784,
      "dotNumber": 74167,
      "prefix": "MC"
    },
    {
      "docketNumber": 966,
      "docketNumberId": 1095889,
      "dotNumber": 74167,
      "prefix": "FF"
    }
  ],
  "retrievalDate": "2026-10-20T16:59:37.914+0000",
  "_links": {
    "self": {
      "href": "https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/mc-numbers"
    }
  }
}
```

### 12.6 BASICs (indicadores de segurança)

**Endpoint:** `GET /carriers/74167/basics`  
**URL chamada:** `https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/basics?webKey=<WEBKEY>`  
**Status:** 200 · **Latência:** 414 ms

> Os campos reais diferem da doc oficial (`basicsPercentile`, `exceededFMCSAInterventionThreshold` etc., e não `percentile`/`rdsvDeficient`). Percentis vêm como `"Not Public"`, e `basicsRunDate` é de 2017.

**Resposta completa:**

```json
{
  "content": [
    {
      "basic": {
        "basicsPercentile": "Not Public",
        "basicsRunDate": "2017-01-27T05:00:00.000+0000",
        "basicsType": {
          "basicsCode": "Unsafe Driving",
          "basicsCodeMcmis": null,
          "basicsId": 11,
          "basicsLongDesc": null,
          "basicsShortDesc": "Unsafe Driving"
        },
        "basicsViolationThreshold": "65",
        "exceededFMCSAInterventionThreshold": "-1",
        "id": {
          "basicsId": 11,
          "dotNumber": 74167
        },
        "measureValue": "0",
        "onRoadPerformanceThresholdViolationIndicator": "Not Public",
        "seriousViolationFromInvestigationPast12MonthIndicator": "N",
        "totalInspectionWithViolation": 0,
        "totalViolation": 0
      },
      "dotNumber": null,
      "_links": {
        "self": {
          "href": "https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/basics/11"
        }
      }
    },
    {
      "basic": {
        "basicsPercentile": "Not Public",
        "basicsRunDate": "2017-01-27T05:00:00.000+0000",
        "basicsType": {
          "basicsCode": "HOS Compliance",
          "basicsCodeMcmis": null,
          "basicsId": 12,
          "basicsLongDesc": null,
          "basicsShortDesc": "Hours-of-Service Compliance"
        },
        "basicsViolationThreshold": "65",
        "exceededFMCSAInterventionThreshold": "-1",
        "id": {
          "basicsId": 12,
          "dotNumber": 74167
        },
        "measureValue": "0",
        "onRoadPerformanceThresholdViolationIndicator": "Not Public",
        "seriousViolationFromInvestigationPast12MonthIndicator": "N",
        "totalInspectionWithViolation": 0,
        "totalViolation": 0
      },
      "dotNumber": null,
      "_links": {
        "self": {
          "href": "https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/basics/12"
        }
      }
    },
    {
      "basic": {
        "basicsPercentile": "Not Public",
        "basicsRunDate": "2017-01-27T05:00:00.000+0000",
        "basicsType": {
          "basicsCode": "Driver Fitness",
          "basicsCodeMcmis": null,
          "basicsId": 13,
          "basicsLongDesc": null,
          "basicsShortDesc": "Driver Fitness"
        },
        "basicsViolationThreshold": "80",
        "exceededFMCSAInterventionThreshold": "-1",
        "id": {
          "basicsId": 13,
          "dotNumber": 74167
        },
        "measureValue": "0",
        "onRoadPerformanceThresholdViolationIndicator": "Not Public",
        "seriousViolationFromInvestigationPast12MonthIndicator": "N",
        "totalInspectionWithViolation": 0,
        "totalViolation": 0
      },
      "dotNumber": null,
      "_links": {
        "self": {
          "href": "https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/basics/13"
        }
      }
    },
    {
      "basic": {
        "basicsPercentile": "Not Public",
        "basicsRunDate": "2017-01-27T05:00:00.000+0000",
        "basicsType": {
          "basicsCode": "Drugs/Alcohol",
          "basicsCodeMcmis": null,
          "basicsId": 14,
          "basicsLongDesc": null,
          "basicsShortDesc": "Controlled Substances/&#8203;Alcohol"
        },
        "basicsViolationThreshold": "80",
        "exceededFMCSAInterventionThreshold": "-1",
        "id": {
          "basicsId": 14,
          "dotNumber": 74167
        },
        "measureValue": "0",
        "onRoadPerformanceThresholdViolationIndicator": "Not Public",
        "seriousViolationFromInvestigationPast12MonthIndicator": "N",
        "totalInspectionWithViolation": 0,
        "totalViolation": 0
      },
      "dotNumber": null,
      "_links": {
        "self": {
          "href": "https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/basics/14"
        }
      }
    },
    {
      "basic": {
        "basicsPercentile": "Not Public",
        "basicsRunDate": "2017-01-27T05:00:00.000+0000",
        "basicsType": {
          "basicsCode": "Vehicle Maint.",
          "basicsCodeMcmis": null,
          "basicsId": 15,
          "basicsLongDesc": null,
          "basicsShortDesc": "Vehicle Maintenance"
        },
        "basicsViolationThreshold": "80",
        "exceededFMCSAInterventionThreshold": "-1",
        "id": {
          "basicsId": 15,
          "dotNumber": 74167
        },
        "measureValue": "0.75",
        "onRoadPerformanceThresholdViolationIndicator": "Not Public",
        "seriousViolationFromInvestigationPast12MonthIndicator": "N",
        "totalInspectionWithViolation": 2,
        "totalViolation": 2
      },
      "dotNumber": null,
      "_links": {
        "self": {
          "href": "https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/basics/15"
        }
      }
    }
  ],
  "retrievalDate": "2026-10-20T16:59:38.324+0000"
}
```

### 12.7 Tipos de carga

**Endpoint:** `GET /carriers/74167/cargo-carried`  
**URL chamada:** `https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/cargo-carried?webKey=<WEBKEY>`  
**Status:** 200 · **Latência:** 402 ms

> `content: []`: este carrier não declarou tipos de carga.

**Resposta completa:**

```json
{
  "content": [],
  "retrievalDate": "2026-10-20T16:59:38.736+0000",
  "_links": {
    "self": {
      "href": "https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/cargo-carried"
    }
  }
}
```

### 12.8 Classificação da operação

**Endpoint:** `GET /carriers/74167/operation-classification`  
**URL chamada:** `https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/operation-classification?webKey=<WEBKEY>`  
**Status:** 200 · **Latência:** 485 ms

> Lista de classificações. Aqui, `Authorized For Hire`.

**Resposta completa:**

```json
{
  "content": [
    {
      "id": {
        "dotNumber": 74167,
        "operationClassId": 1
      },
      "operationClassDesc": "Authorized For Hire"
    }
  ],
  "retrievalDate": "2026-10-20T16:59:39.164+0000",
  "_links": {
    "self": {
      "href": "https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/operation-classification"
    }
  }
}
```

### 12.9 Busca por nome

**Endpoint:** `GET /carriers/name/AIR EXPRESS INTERNATIONAL USA INC?start=0&size=3`  
**URL chamada:** `https://mobile.fmcsa.dot.gov/qc/services/carriers/name/AIR%20EXPRESS%20INTERNATIONAL%20USA%20INC?webKey=<WEBKEY>&start=0&size=3`  
**Status:** 200 · **Latência:** 2201 ms

> Mesmo formato do docket (`content` é lista de `{_links, carrier}`), com paginação por `start`/`size`. Não usar para verificação.

**Resposta completa:**

```json
{
  "content": [
    {
      "_links": {
        "basics": {
          "href": "https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/basics"
        },
        "cargo carried": {
          "href": "https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/cargo-carried"
        },
        "operation classification": {
          "href": "https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/operation-classification"
        },
        "docket numbers": {
          "href": "https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/docket-numbers"
        },
        "carrier active-For-hire authority": {
          "href": "https://mobile.fmcsa.dot.gov/qc/services/carriers/74167/authority"
        }
      },
      "carrier": {
        "allowedToOperate": "Y",
        "bipdInsuranceOnFile": "5000",
        "bipdInsuranceRequired": "u",
        "bipdRequiredAmount": "750",
        "bondInsuranceOnFile": "75",
        "bondInsuranceRequired": "Y",
        "brokerAuthorityStatus": "A",
        "cargoInsuranceOnFile": "0",
        "cargoInsuranceRequired": "u",
        "carrierOperation": {
          "carrierOperationCode": "A",
          "carrierOperationDesc": "Interstate"
        },
        "censusTypeId": {
          "censusType": "C",
          "censusTypeDesc": "CARRIER",
          "censusTypeId": 1
        },
        "commonAuthorityStatus": "A",
        "contractAuthorityStatus": "A",
        "crashTotal": 0,
        "dbaName": "DHL GLOBAL FORWARDING",
        "dotNumber": 74167,
        "driverInsp": 7,
        "driverOosInsp": 0,
        "driverOosRate": 0,
        "driverOosRateNationalAverage": "5.51",
        "ein": 61113864,
        "fatalCrash": 0,
        "hazmatInsp": 0,
        "hazmatOosInsp": 0,
        "hazmatOosRate": 0,
        "hazmatOosRateNationalAverage": "4.5",
        "injCrash": 0,
        "isPassengerCarrier": null,
        "issScore": null,
        "legalName": "AIR EXPRESS INTERNATIONAL USA INC",
        "mcs150Outdated": "N",
        "oosDate": null,
        "oosRateNationalAverageYear": "2009-2010",
        "phyCity": "DARIEN",
        "phyCountry": "US",
        "phyState": "CT",
        "phyStreet": "120 TOKENEKE RD",
        "phyZipcode": "06820-4825",
        "reviewDate": "1987-03-20",
        "reviewType": "S",
        "safetyRating": "C",
        "safetyRatingDate": "1990-03-02",
        "safetyReviewDate": "1987-03-20",
        "safetyReviewType": "S",
        "snapshotDate": null,
        "statusCode": "A",
        "totalDrivers": 181,
        "totalPowerUnits": 102,
        "towawayCrash": 0,
        "vehicleInsp": 3,
        "vehicleOosInsp": 0,
        "vehicleOosRate": 0,
        "vehicleOosRateNationalAverage": "20.72"
      }
    }
  ],
  "retrievalDate": "2026-10-20T16:59:39.630+0000"
}
```

---

## 13. Como obter o `telephone` registrado (investigação, 2026-10-20)

A QCMobile **não devolve telefone** (seção 12). Testamos as alternativas com o MC 193047 / USDOT 74167. Em todas as fontes que trazem telefone, o valor foi o mesmo: **(305) 302-3989**, vindo do cadastro MCS-150 (data 07/23/2026).

### 13.1 Fontes testadas

| # | Fonte | Telefone? | Como consultar | Resultado |
|---|---|---|---|---|
| A | **Company Census File** (Socrata `az4n-8mr2`) | **Sim**: `phone`, `cell_phone`, `fax`, `email_address` | REST JSON, **sem webKey** | **Recomendada.** Atualizada em 2026-10-19 |
| B | **SMS Input - Motor Carrier Census** (Socrata `kjg3-diqy`) | **Sim**: `telephone` (formatado), `fax`, `email_address` | REST JSON, por `dot_number` | Alternativa e fonte de conferência. Atualizada em 2026-10-13 (mensal, só carriers ativos) |
| C | **SAFER Company Snapshot** (`safer.fmcsa.dot.gov`) | Sim (campo `Phone`) | HTML, scraping | Funciona, mas frágil (HTML), sem contrato de API e sem `robots.txt` (404). **Só para conferência manual** |
| D | **L&I** (`li-public.fmcsa.dot.gov`) | Não confirmado | HTML, formulário | A consulta direta por URL só devolveu o formulário de busca. Descartada, sem ganho sobre A |
| E | QCMobile: `/contacts`, `/census`, `/phone`, `/mcs150`, `/insurance` | Não | — | Todos **404** ("There is no resource for path"). `/mc-numbers` deu 504 |
| F | Download em lote do census (CSV) | Sim | arquivo completo | Opção se a latência da A não servir (ver 13.4) |

### 13.2 Fonte recomendada: Company Census File (A)

Endpoint base: `https://data.transportation.gov/resource/az4n-8mr2.json`

Consulta por DOT (que já vem da QCMobile):
```
GET /resource/az4n-8mr2.json?dot_number=74167&$select=dot_number,legal_name,phone,cell_phone,email_address,mcs150_date,status_code
```

Consulta direta por MC, pulando a QCMobile (o MC pode estar em qualquer um dos 3 slots de docket):
```
GET /resource/az4n-8mr2.json
    ?$where=(docket1prefix='MC' AND docket1='193047') OR (docket2prefix='MC' AND docket2='193047') OR (docket3prefix='MC' AND docket3='193047')
    &$select=dot_number,legal_name,phone,docket1,docket2,docket3
```

Resposta real (com `$select=dot_number,legal_name,phone,cell_phone,email_address,docket1,docket2,docket3,mcs150_date,status_code`):
```json
[{"dot_number":"74167","legal_name":"AIR EXPRESS INTERNATIONAL USA INC","phone":"3053023989","cell_phone":"6148655914","email_address":"mark.muse@dhl.com","docket1":"193047","docket2":"193047","docket3":"867610","mcs150_date":"20260723","status_code":"A"}]
```

Resposta da fonte B (`/resource/kjg3-diqy.json?dot_number=74167&$select=telephone,mcs150_date`):
```json
[{"telephone":"(305) 302-3989","mcs150_date":"23-JUL-26"}]
```

**Observações dos testes:**
- `phone` vem só com dígitos (`3053023989`), sem formato. Normalizar para E.164 (`+13053023989`).
- Existe também `cell_phone`. Aqui são números **diferentes** (305 e 614). Qual é o "registrado"? Regra proposta: usar `phone`; `cell_phone` só como sinal auxiliar, nunca como destino do OTP, sem validação do cliente.
- MC inexistente (`99999999`) → `200` com `[]`. MC com zero à esquerda (`0193047`) → `[]` (normalizar antes).
- O MC 867610 (docket 3, inativo) resolve para o mesmo DOT, então a busca por qualquer docket do carrier funciona.
- Data format: `mcs150_date` vem como `YYYYMMDD` no census A e `DD-MON-YY` no B.
- Sem app token a API funcionou nos testes. Para produção, criar um **app token Socrata** (gratuito) para evitar throttling.

### 13.3 Riscos e limites

| Risco | Detalhe | Mitigação |
|---|---|---|
| **Telefone desatualizado** | O número é o do último MCS-150. Ele é atualizado a cada 24 meses (ou quando o carrier altera) | Exibir `mcs150_date`; regra de idade máxima (por exemplo, rejeitar > 24 meses → humano) |
| **Fraude por troca de cadastro** | Quem toma uma identidade de carrier pode alterar o telefone no registro da FMCSA e passar no OTP | Cruzar com a data do último MCS-150 e com a idade da autoridade; alertar se o telefone mudou nos últimos N dias (exige guardar histórico, a FMCSA não dá) |
| **Telefone fixo ou de escritório** | `phone` pode ser fixo e não receber SMS | OTP por **voz** como fallback; fila humana se falhar |
| **Dados defasados em relação à QCMobile** | A: atualizado diariamente (19/10). B: mensal | Preferir A; usar B só para conferência |
| **Quem é o titular** | O telefone pode ser de um despachante ou terceiros | Pergunta ao cliente (seção 11) |
| **Disponibilidade** | `data.transportation.gov` teve latência de 0,7 a 7,5 s nas consultas, conforme o filtro | Ver 13.4 |

### 13.4 Desempenho e arquitetura sugerida

Latência medida (uma amostra por consulta): por `docket1=` 1,3 s; `$where` com OR nos 3 slots 3,4 s; por docket3 5,7 s; por `dot_number` ≈ 1 s. Filtros em colunas sem índice são lentos.

Fluxo recomendado:
1. QCMobile `GET /carriers/docket-number/{mc}` → obtém o DOT e decide a elegibilidade (como na seção 7).
2. Só se aprovado, consultar o census **por `dot_number`** (mais rápido) para obter `phone` e `mcs150_date`.
3. Cache do telefone por DOT (TTL de 24 h, igual ao do resultado aprovado).
4. Se a latência ou a disponibilidade incomodar, sincronizar o census (CSV/Socrata) para uma tabela local diária indexada por `dot_number` e `docket*`. Fica em < 50 ms e fora do caminho crítico da ligação.
5. Se o census falhar ou não tiver `phone` → `no_registered_phone` / `fmcsa_unavailable` e fila humana (fail-closed, seção 7).

### 13.5 Impacto no desenho atual

- Seção 4.1: `telephone` **não existe** na QCMobile. Trocar a fonte para o census (`phone`).
- Seção 6, passo 5: "obter o telephone" passa a ser uma 2ª chamada, ao census, por DOT.
- Seção 9, item 3 ("confirmar se o telephone vem no docket-number"): **resolvido, não vem**.
- Seção 11: adicionar (a) idade máxima aceitável do MCS-150, (b) usar `cell_phone` ou não, (c) política de OTP por voz.

