# Resultados dos Testes Iniciais: FMCSA e TMS Legado

> Primeira sondagem real dos sistemas externos que a Integration API vai consumir. O plano está em [`discovery/PLANO_DE_TESTES.md`](./discovery/PLANO_DE_TESTES.md), e as respostas brutas (com segredos e telefones mascarados) estão em `discovery/results/`.
>
> **Data:** 2026-10-13 · **Ambiente:** macOS, Python 3.14 (só stdlib) · **Status:** FMCSA ✅ executada (22 testes) · TMS ⏸️ **pendente do `TMS_TOKEN`** (script pronto; só a conectividade foi verificada)

---

## 1. Resumo executivo

| # | Descoberta | Impacto no desenho |
|---|---|---|
| 1 | 🚨 **A FMCSA QCMobile não devolve telefone em nenhum endpoint testado** (carrier, docket, authority, oos, docket-numbers, basics). O campo `telephone` da documentação não existe na resposta real. | **Quebra a premissa do OTP** ("enviar SMS ao telefone registrado no FMCSA"). Precisa de uma nova fonte para o telefone registrado (ver §5). É o ponto mais urgente para levar ao cliente. |
| 2 | ⚠️ **`allowedToOperate = "Y"` não basta.** O DOT 1198688 vem com `allowedToOperate: Y`, mas `statusCode: I` (inativo) e **sem nenhuma autoridade**. | As regras R3 (autoridade ativa) e R5 (`statusCode == "A"`) passam de "inferidas" a **obrigatórias**. Com só R1, um carrier inativo seria aprovado. |
| 3 | O nome real do campo é **`allowedToOperate`** (e não `allowToOperate`), e o de OOS é **`oosDate`**. | Confirma os aliases previstos no doc. |
| 4 | Os status de autoridade (`commonAuthorityStatus`, `contractAuthorityStatus`, `brokerAuthorityStatus`) **vêm no próprio objeto do carrier**, com valores `A` / `I` / `N` / `null`. | A verificação cabe em **1 chamada** (`docket-number`); o `/authority` fica opcional. |
| 5 | **MC inexistente → HTTP 200 com `content: []`**, não 404. **DOT inexistente ou inválido → HTTP 500** com mensagem genérica. | O parser precisa tratar 200 vazio como `mc_not_found`. Um 500 **não é** necessariamente indisponibilidade: o retry em 5xx tem que ser limitado para não insistir em DOT inválido. |
| 6 | **webKey inválida → HTTP 404 "Webkey not found"**, e não 401 como diz a documentação. | Mapear 404 com essa mensagem para alerta crítico de credencial, sem confundir com "não encontrado". |
| 7 | O `docket-number` aceita **zeros à esquerda** (`00136818` → mesmo carrier). | A normalização pode só tirar o prefixo `MC` e os não-dígitos. |
| 8 | Latência: **~0,5 s** por chamada por DOT/MC; a busca por nome levou de 2 a 7,6 s. | Um timeout de 3 s serve para o fluxo de verificação. **Não usar a busca por nome no caminho da ligação.** |
| 9 | TMS: host e porta alcançáveis; um token inválido devolve `ERR\|CODE:AUTH_FAILED\|MSG:Invalid or missing auth token` em ~0,7 s. | Confirma o transporte, o terminador `\r\n` e o formato de erro do manual. |

---

## 2. Como rodar

```bash
python3 discovery/run_fmcsa_probes.py   # usa FMCSA_API_KEY do .env
python3 discovery/run_tms_probes.py     # exige TMS_TOKEN no .env (ainda não configurado)
```
Os scripts leem os segredos do `.env`, mascaram `webKey=`/`AUTH:` e telefones e gravam em `discovery/results/*.json`. A ausência de vazamento foi conferida com `grep` da chave real nos arquivos gerados (0 ocorrências).

---

## 3. FMCSA: resultados por endpoint

**Carriers usados:** os números foram descobertos na própria API, na primeira rodada exploratória.

| Papel | DOT | MC | Nome | Perfil |
|---|---|---|---|---|
| Ativo | 54283 | 136818 | SWIFT TRANSPORTATION COMPANY OF ARIZONA LLC | `statusCode A`, common/contract/broker `A`, BIPD 1000 em dia |
| Inativo | 1198688 | — | SWIFT TRANSPORTATION (Denver, CO) | `statusCode I`, sem autoridade, **`allowedToOperate Y`** |
| Inexistente | 99999999999 / `abc` | 99999999 | — | — |

### 3.1 Tabela-resumo

Legenda: ✅ como esperado · ⚠️ funciona, mas diverge da documentação ou da expectativa

| ID | Endpoint | Entrada | Esperado | Observado | Latência | |
|---|---|---|---|---|---:|:-:|
| FM-NAME-1 | `/carriers/name/{name}` | `greyhound` | 200, lista | 200, **20** itens | 7648 ms | ✅ |
| FM-NAME-2 | `/carriers/name/{name}` | `swift transportation`, `size=5` | 200, até 5 | 200, 5 itens (paginação ok) | 2205 ms | ✅ |
| FM-NAME-3 | `/carriers/name/{name}` | `zzzqqxxnotacarrier` | vazio ou 404 | 200, `content: []` | 2148 ms | ✅ |
| FM-DOT-1 | `/carriers/{dot}` | 54283 | 200, objeto | 200, `content` = objeto `{carrier, _links}` | 515 ms | ✅ |
| FM-DOT-2 | `/carriers/{dot}` | 1198688 | 200, objeto | 200; **`allowedToOperate Y` + `statusCode I`** | 527 ms | ✅ |
| FM-DOT-3 | `/carriers/{dot}` | `abc` | 400/404 | **500**, `content` = texto de erro + `Error ID` | 511 ms | ⚠️ |
| FM-AUTH-1 | `/carriers/{dot}/authority` | 54283 | 200 | 200, 1 `carrierAuthority` (common/contract/broker `A`, `authorizedForProperty Y`) | 581 ms | ✅ |
| FM-AUTH-2 | `/carriers/{dot}/authority` | 1198688 | 200 | 200, `content: []` (nenhuma autoridade) | 468 ms | ✅ |
| FM-AUTH-3 | `/carriers/{dot}/authority` | 99999999999 | 404/vazio | **500** | 523 ms | ⚠️ |
| FM-OOS-1 | `/carriers/{dot}/oos` | 54283 | 200 | 200, `content: []` (sem OOS) | 535 ms | ✅ |
| FM-OOS-2 | `/carriers/{dot}/oos` | 1198688 | 200 | 200, `content: []` | 520 ms | ✅ |
| FM-OOS-3 | `/carriers/{dot}/oos` | 99999999999 | 404/vazio | **500** | 517 ms | ⚠️ |
| FM-DKTS-1 | `/carriers/{dot}/docket-numbers` | 54283 | 200 | 200, `[{prefix: MC, docketNumber: 136818}]` | 529 ms | ✅ |
| FM-DKTS-2 | `/carriers/{dot}/docket-numbers` | 1198688 | 200 | 200, `content: []` | 527 ms | ✅ |
| FM-DKTS-3 | `/carriers/{dot}/docket-numbers` | 99999999999 | 404/vazio | **500** | 527 ms | ⚠️ |
| FM-BASICS-1 | `/carriers/{dot}/basics` | 54283 | 200 | 200, 5 BASICs, todos `basicsPercentile: "Not Public"` | 528 ms | ✅ |
| FM-BASICS-2 | `/carriers/{dot}/basics` | 1198688 | 200 | 200, `content: []` | 517 ms | ✅ |
| FM-BASICS-3 | `/carriers/{dot}/basics` | 99999999999 | 404/vazio | **500** | 521 ms | ⚠️ |
| FM-DOCKET-1 | `/carriers/docket-number/{mc}` | 136818 | 200, mesmo DOT | 200, lista com 1 `{carrier}` → DOT 54283 | 578 ms | ✅ |
| FM-DOCKET-2 | `/carriers/docket-number/{mc}` | `00136818` | mesmo carrier | 200, DOT 54283 (normaliza zeros) | 505 ms | ✅ |
| FM-DOCKET-3 | `/carriers/docket-number/{mc}` | 99999999 | 404 ou vazio | 200, `content: []` | 528 ms | ✅ |
| FM-KEY-X | `/carriers/name/greyhound` | webKey inválida | 401 | **404**, `"Webkey not found"` | 486 ms | ⚠️ |

**Totais:** 22 testes, 16 ✅ e 6 ⚠️ (todos os ⚠️ são códigos de erro diferentes dos documentados; nenhum é falha de execução). Latência dos endpoints por DOT/MC: p50 ≈ 527 ms, máx. 581 ms.

### 3.2 Detalhe e explicação por endpoint

**`GET /carriers/docket-number/{mc}`: endpoint principal da verificação**
- Envelope: `{"content": [ {"carrier": {...}, "_links": {...}} ], "retrievalDate": "..."}`. É uma **lista**, mesmo com um único resultado.
- O objeto `carrier` é **idêntico** ao de `/carriers/{dot}` (mesmas ~60 chaves). Uma única chamada já traz tudo de que a regra de elegibilidade precisa: `allowedToOperate`, `statusCode`, `commonAuthorityStatus`, `contractAuthorityStatus`, `brokerAuthorityStatus`, `oosDate`, `bipdInsuranceOnFile`/`bipdRequiredAmount`, `legalName`, `dbaName`, `phy*`.
- O objeto **não traz `mcNumber`**. A regra R6 ("o MC retornado bate com o informado") tem que ser feita via `/docket-numbers` ou aceitar o próprio filtro como prova.
- MC inexistente → `200` + `content: []` ⇒ `mc_not_found`.
- **Não há telefone** (ver §5).

**`GET /carriers/{dot}`**
- Envelope com `content` = **objeto** (não lista). O parser tem que aceitar os dois formatos.
- Tipos mistos: `dotNumber` vem como **inteiro**; `bipdInsuranceOnFile` e `bipdRequiredAmount` como **string** (`"1000"`, em milhares de USD); `cargoInsuranceRequired` veio como `"u"`. Coerção tolerante confirmada como necessária.
- Os campos nulos **vêm como `null`**, não ausentes, ao contrário do que a doc sugere ("elements only appear if they have value"). Tratar `null` e ausente da mesma forma.
- Campos aninhados: `carrierOperation {carrierOperationCode, carrierOperationDesc}` e `censusTypeId {censusType, censusTypeDesc}`.

**`GET /carriers/{dot}/authority`**
- Lista de `{"carrierAuthority": {...}}` com `commonAuthorityStatus`, `contractAuthorityStatus`, `brokerAuthorityStatus`, `authorizedForProperty`, `authorizedForBroker`, `authorizedForPassenger`, `authorizedForHouseholdGoods`, `docketNumber` e `prefix`.
- `authorizedForProperty: Y` é um sinal mais direto de "pode transportar carga" que o status common/contract. É útil como confirmação **opcional**.
- Carrier sem autoridade → `content: []`.

**`GET /carriers/{dot}/oos`**
- Os dois carriers testados não têm OOS, então só confirmamos o formato vazio (`content: []`). **O formato com registro de OOS continua em aberto**: precisamos de um DOT com ordem ativa.

**`GET /carriers/{dot}/docket-numbers`**
- `[{prefix: "MC", docketNumber: 136818, dotNumber: 54283, docketNumberId}]`. É a forma de cumprir a R6 (MC ↔ DOT).
- Curiosidade: o `_links.self` aponta para `/carriers/{dot}/mc-numbers`, um alias que também responde.

**`GET /carriers/{dot}/basics`**
- 5 BASICs: Unsafe Driving, HOS Compliance, Driver Fitness, Controlled Substances/Alcohol e Vehicle Maintenance.
- Campos reais diferentes dos documentados: `basicsPercentile` (veio `"Not Public"`), `exceededFMCSAInterventionThreshold` (`"-1"`), `onRoadPerformanceThresholdViolationIndicator` e `seriousViolationFromInvestigationPast12MonthIndicator`. **Não** vêm `rdDeficient`/`rdsvDeficient`/`svDeficient` com esses nomes.
- O `basicsRunDate` do carrier testado é de **2017**: dado velho e não público, então **pouco útil como sinal de risco**. Recomendação: tirar BASICs do fluxo da ligação.
- O texto pode vir com entidade HTML (`Controlled Substances/&#8203;Alcohol`).

**`GET /carriers/name/{name}`**
- Sem `size`, devolveu 20 itens (a doc fala em limite de 50). Com `size=5`, respeitou o valor.
- Lenta (2 a 7,6 s) e ambígua: a busca por "swift transportation" trouxe 5 empresas diferentes, uma delas inativa e com `allowedToOperate Y`. **Confirma: não usar para verificação.**

**Autenticação**
- A webKey inválida gera `HTTP 404` com `content: "Webkey not found"`, e não 401.

---

## 4. TMS legado: status

| Item | Resultado |
|---|---|
| Conectividade (`TMS_HOST:TMS_PORT` do `.env`) | ✅ TCP aberto; resposta em ~680 ms |
| `DEBUG_ECHO` com token inválido (equivale ao TMS-ECHO-3) | ✅ `ERR\|CODE:AUTH_FAILED\|MSG:Invalid or missing auth token\r\n`. Confirma o formato `ERR\|CODE:\|MSG:` e o `\r\n` do manual. |
| Os outros 12 testes (ECHO-1/2, QUERY-1/2/3a/3b, GET-1/2/3, BOOK-1/2/3) | ⏸️ **Pendentes**: falta o `TMS_TOKEN` no `.env` |

O script `discovery/run_tms_probes.py` já implementa:
- uma conexão por comando, com timeout de 2 s no connect e 8 s na leitura, fechando do nosso lado ao ler `END`/`ERR`;
- a classificação de cada tentativa (`ok`, `timeout`, `partial`, `malformed`, `empty_eof`, delayed close), com até 2 retries **registrados**, o que já dá a primeira medida da taxa de falha injetada;
- `LOAD_QUERY` → os `LOAD_ID` reais alimentam o `LOAD_GET` (inclui a detecção do `MAX_BUY`);
- `LOAD_BOOK` **só com `LD9999999999`** e um `assert` que aborta se esse id coincidir com uma carga real. **Nenhuma carga é consumida.**

**Próximo passo:** adicionar `TMS_TOKEN=...` ao `.env` e rodar `python3 discovery/run_tms_probes.py`. Depois, este documento ganha a seção 4 completa.

---

## 5. 🚨 Ponto crítico: origem do telefone para o OTP

O requisito de compliance diz que o OTP deve ir para o **telefone registrado do carrier**, e os docs assumiam que ele viria da FMCSA. **A QCMobile não expõe telefone.** Opções para levar ao cliente:

1. **Dataset público do FMCSA Census (Company Census File / SAFER)**: inclui o telefone do MCS-150. É sincronizado periodicamente para uma tabela local e consultado por DOT. Mantém o "telefone registrado no FMCSA" como fonte. *Recomendado para avaliar primeiro.*
2. **Telefone cadastrado no próprio sistema da Crystal Lane** (carriers que já trabalham com eles), com cadastro manual verificado para os novos.
3. **Demo/PoC:** a allowlist de MCs de teste com override de telefone (já prevista no plano) passa a ser **obrigatória**, e não só conveniência.

Em qualquer opção, o princípio continua: **nunca usar um número falado na ligação**.

---

## 6. Itens "⚠️ A validar" resolvidos

### `Integracao FMCSA.md`
| Item do doc | Resultado |
|---|---|
| Envelope `content` lista vs objeto | ✅ Confirmado: lista no `docket-number`/`name`, objeto no `/carriers/{dot}` |
| `allowToOperate` vs `allowedToOperate` | ✅ Real: **`allowedToOperate`** |
| `outOfServiceDate` vs `oosDate` | ✅ Real: **`oosDate`** |
| Status de autoridade no objeto carrier | ✅ Confirmado (`A`/`I`/`N`/`null`) |
| `statusCode` | ✅ Existe e é **essencial** (pega um inativo com `allowedToOperate Y`) |
| Campos de seguro | ✅ Existem; valores em string |
| `telephone` vem no docket? | ❌ **Não vem em nenhum endpoint** |
| Formato do `docketNumber` | ✅ Só dígitos; zeros à esquerda são aceitos |
| MC inexistente: 404 ou vazio? | ✅ **200 + `content: []`** |
| Rate limit e latência | ⚠️ Nenhum 429 em 22 chamadas sequenciais; ~0,5 s por DOT/MC. Rate limit real ainda não medido |
| Tipos | ✅ Mistos (int e string), confirmando a necessidade de coerção |
| Auth inválida = 401 | ❌ É **404 "Webkey not found"** |

### `Integracao TMS Legado.md`
| Item do doc | Resultado |
|---|---|
| Formato de erro / `AUTH_FAILED` | ✅ Confirmado |
| `MAX_BUY` para o nosso token, padding do `STATUS`, `EQTYPE`, filtros, `MAX_RESULTS`, fuso | ⏸️ Pendentes do token |

---

## 7. Recomendações de atualização dos documentos (não aplicadas)

**`Integracao FMCSA.md`**
1. §4.1/§6/§10: remover `telephone` como fonte do OTP e apontar para a §5 deste documento.
2. §7: promover R3 e R5 a **obrigatórias** (com o exemplo do DOT 1198688), e trocar a R6 para usar `/docket-numbers`, já que o objeto do carrier não traz `mcNumber`.
3. §2.2/§3: um webKey inválido retorna 404; um DOT inválido retorna 500; MC inexistente retorna 200 vazio.
4. §8: o retry em 5xx deve ter no máximo 1 tentativa, porque 500 também significa "DOT inválido".
5. §4.2: corrigir os nomes dos campos de BASICs e rebaixar BASICs a "fora do fluxo da ligação".
6. §4.3: nulos vêm como `null`, não ausentes.

**`Integracao TMS Legado.md`**: atualizar depois da rodada com token.

---

## 8. Próximos passos

1. Adicionar `TMS_TOKEN` ao `.env` e rodar os testes do TMS.
2. Encontrar MCs reais para os cenários que faltam: **OOS ativo** (formato de `/oos` com registro), **broker-only** e **autoridade revogada**. Esses MCs viram a lista de MCs de QA.
3. Decidir com o cliente a **fonte do telefone para o OTP** (§5).
4. Medir o rate limit da FMCSA com ~50 chamadas espaçadas.
5. Transformar os JSONs de `discovery/results/` em fixtures dos testes unitários do parser.
