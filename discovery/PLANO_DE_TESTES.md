# Plano de Testes Iniciais: TMS Legado e FMCSA

> Sondagem dos endpoints que provavelmente vamos usar na Integration API, com **3 testes por endpoint**. O objetivo é confirmar o comportamento real do fio/da API, resolver os pontos **⚠️ A validar** de `Integracao TMS Legado.md` e `Integracao FMCSA.md` e gerar fixtures brutas para os testes do adapter.
>
> Os resultados ficam em [`../RESULTADOS_TESTES_INICIAIS.md`](../RESULTADOS_TESTES_INICIAIS.md) e as respostas brutas em `results/`.

## Regras gerais

- **Segredos:** `TMS_TOKEN`, `TMS_HOST`, `TMS_PORT` e `FMCSA_API_KEY` são lidos do `.env`. O token (`AUTH:`) e a `webKey=` são **mascarados** em tudo que é gravado.
- **Sem efeitos colaterais:** o `LOAD_BOOK` é testado **apenas com casos de erro**, sempre com um `LOAD_ID` inexistente ou sem campos obrigatórios, para não consumir nenhuma carga (uma reserva bem-sucedida é irreversível para o token).
- **TMS:** uma conexão TCP por comando; timeout de 2 s no connect e 8 s na leitura; a conexão é fechada do nosso lado ao ler `END` ou `ERR|…`. Falhas transitórias (timeout, resposta parcial ou malformada) ganham até 2 retries, e **todas as tentativas são registradas**.
- **FMCSA:** HTTPS GET com timeout de 10 s. Os `dotNumber`/`mcNumber` usados nos testes são **descobertos em tempo de execução** a partir da busca por nome, para não chutar números.
- **Telefones** da FMCSA são gravados mascarados (só os 4 últimos dígitos).

## Como rodar

```bash
python3 discovery/run_tms_probes.py     # exige TMS_TOKEN no .env
python3 discovery/run_fmcsa_probes.py
```

---

## 1. TMS legado (TCP, protocolo `CMD:…|AUTH:…|…`)

| ID | Comando | Entrada | Objetivo | Resultado esperado |
|---|---|---|---|---|
| TMS-ECHO-1 | `DEBUG_ECHO` | `MSG:HELLO` | token e conectividade | `ECHO\|AUTH:OK\|FIELDS_PARSED:3\|MSG:HELLO` + `END` |
| TMS-ECHO-2 | `DEBUG_ECHO` | `MSG`, `A`, `B`, `ZZZ_UNKNOWN` (6 pares) | contrato do encoder; campo desconhecido é contado | `FIELDS_PARSED:6` |
| TMS-ECHO-3 | `DEBUG_ECHO` | token inválido | formato do erro de autenticação | `ERR\|CODE:AUTH_FAILED\|…` |
| TMS-QUERY-1 | `LOAD_QUERY` | `ORIG_STATE:GA`, `MAX_RESULTS:50` | volume, `EQTYPE` reais, padding do `STATUS`, teto do `MAX_RESULTS` | registros + `END` |
| TMS-QUERY-2 | `LOAD_QUERY` | `ORIG_STATE` + `DEST_STATE` + `EQTYPE` de uma carga vista no QUERY-1 | filtros combinados realmente filtram | subconjunto do QUERY-1, todos batendo com os filtros |
| TMS-QUERY-3a | `LOAD_QUERY` | sem filtros | consulta sem filtro | `ERR\|CODE:MISSING_FIELD` |
| TMS-QUERY-3b | `LOAD_QUERY` | `ORIG_STATE:GA`, `EQTYPE:XYZ` | equipamento inválido | só `END` (vazio silencioso) |
| TMS-GET-1 | `LOAD_GET` | 1º `LOAD_ID` do QUERY-1 | todos os campos; **o token recebe `MAX_BUY`?** | registro completo + `END` |
| TMS-GET-2 | `LOAD_GET` | 2º `LOAD_ID` do QUERY-1 | relação `MAX_BUY` vs `RATE`; formato das datas | registro completo + `END` |
| TMS-GET-3 | `LOAD_GET` | `LD9999999999` | id inexistente | `ERR\|CODE:UNKNOWN_LOAD` |
| TMS-BOOK-1 | `LOAD_BOOK` | `LD9999999999`, `MC_NUM:123456`, `AGREED_RATE:1000` | id inexistente | `ERR\|CODE:UNKNOWN_LOAD` |
| TMS-BOOK-2 | `LOAD_BOOK` | `LD9999999999`, `MC_NUM:123456` (sem `AGREED_RATE`) | campo obrigatório faltando | `ERR\|CODE:MISSING_FIELD` |
| TMS-BOOK-3 | `LOAD_BOOK` | `LD9999999999`, `MC_NUM:123456`, `AGREED_RATE:0` | precedência entre `INVALID_RATE` e `UNKNOWN_LOAD` | um dos dois (observar qual) |

Para cada teste são registrados: linha enviada (token mascarado), linhas recebidas, presença de `END`, latência, classificação (`ok`, `timeout`, `partial`, `malformed`, `delayed_close`, `error_response`) e veredito (✅ como esperado / ⚠️ divergente / ❌ falhou).

## 2. FMCSA (QCMobile REST, `https://mobile.fmcsa.dot.gov/qc/services/`)

| ID | Endpoint | Entrada | Objetivo |
|---|---|---|---|
| FM-NAME-1 | `/carriers/name/{name}` | `greyhound` | exemplo oficial; formato da lista |
| FM-NAME-2 | `/carriers/name/{name}` | carrier de carga, `start=0&size=5` | paginação; fonte de DOT/MC de carga |
| FM-NAME-3 | `/carriers/name/{name}` | `zzzqqxxnotacarrier` | formato da resposta vazia |
| FM-DOCKET-1 | `/carriers/docket-number/{mc}` | MC ativo descoberto | envelope; `allowedToOperate` vs `allowToOperate`; **`telephone` vem?** |
| FM-DOCKET-2 | `/carriers/docket-number/{mc}` | o mesmo MC com zeros à esquerda | normalização da entrada |
| FM-DOCKET-3 | `/carriers/docket-number/{mc}` | `99999999` | MC inexistente: 404 ou `content` vazio? |
| FM-DOT-1 | `/carriers/{dot}` | DOT do carrier de carga | campos de autoridade, `statusCode`, seguro, `telephone` |
| FM-DOT-2 | `/carriers/{dot}` | DOT da Greyhound (outro perfil) | comparação entre perfis |
| FM-DOT-3 | `/carriers/{dot}` | `abc` | DOT inválido: 400/404? |
| FM-AUTH-1..3 | `/carriers/{dot}/authority` | DOT de carga, DOT Greyhound, `99999999999` | detalhe de autoridade (common/contract/broker) |
| FM-OOS-1..3 | `/carriers/{dot}/oos` | idem | formato do out-of-service |
| FM-DKTS-1..3 | `/carriers/{dot}/docket-numbers` | idem | vínculo MC ↔ DOT |
| FM-BASICS-1..3 | `/carriers/{dot}/basics` | idem | estrutura dos BASICs |
| FM-KEY-X | `/carriers/name/greyhound` | `webKey` inválida | formato do 401 (teste extra) |

Para cada teste são registrados: status HTTP, latência, chaves de topo do JSON, tipo do `content` (lista/objeto/nulo/vazio), campos presentes no objeto `carrier` e veredito.

## 3. Critérios de sucesso da sondagem

1. Todos os testes executados e registrados em `results/*.json`.
2. Nenhum segredo nos arquivos gerados (verificado por `grep`).
3. Nenhum `LOAD_BOOK` enviado com um `LOAD_ID` real.
4. Cada item **⚠️ A validar** coberto pelos testes recebe uma resposta (confirmado, divergente ou ainda em aberto) no documento de resultados.
