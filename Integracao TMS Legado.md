# Integração com o TMS Legado (LTMS)

> Guia de como o sistema vai conversar com o TMS legado da HappyRobot. Serve de referência para desenhar os processos (workflow do agente, Integration API, Twin).
>
> **Fonte:** *Legacy TMS Protocol Reference* (FORM-9100, REV 1.0), em `fde-challenge-candidate-handbook-production.up.railway.app`, com as páginas Spec, Fault behavior, LOAD_QUERY, LOAD_GET, LOAD_BOOK, DEBUG_ECHO, Token e Changelog. As larguras dos campos foram **contadas nas transcrições de exemplo** (o manual não as lista e manda contar).
>
> O manual se define como *"field manual, not a formal specification. Where behavior is not stated, the wire is authoritative"*. Tudo que estiver marcado como **⚠️ A validar** precisa ser confirmado com sondagens reais no servidor (seção 9).
>
> **Status (2026-10-20):** a 1ª rodada de sondagens com o `TMS_TOKEN` real já rodou (`discovery/run_tms_probes.py`, resultado bruto em `discovery/results/tms_results.json`). Onde o servidor real **diverge do manual**, o texto abaixo traz a marca **✅ Observado** e vale o que foi observado. Resumo das divergências na seção 1.1.

---



## 1. Resumo para quem vai desenhar os processos


| Tema                           | O que muda no desenho                                                                                                                                          |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Transporte**                 | TCP cru, ASCII, uma requisição por conexão. Nada de REST: precisamos de um **adapter** que exponha uma API HTTP para o workflow.                               |
| **Falhas silenciosas**         | O servidor injeta timeouts, respostas truncadas, malformadas e conexões que não fecham, **sem sinalizar nada**. O adapter precisa validar tudo pelo protocolo. |
| `MAX_BUY` **=** `max_rate`     | Vem no `LOAD_GET`, mas **só para tokens com permissão**. É o teto da negociação, fica só no backend e **nunca** vai para o agente.                             |
| **O TMS não aplica o teto**    | No exemplo oficial, uma reserva a **2200** foi aceita numa carga com `MAX_BUY` **1950**. A regra do teto é **nossa**, não do TMS.                              |
| **Reserva por token**          | Todas as ligações usam o mesmo token, então o TMS não distingue os carriers. Concorrência e idempotência ficam por nossa conta.                                |
| `ALREADY_BOOKED` **é ambíguo** | Depois de uma reserva cuja resposta se perdeu, um retry recebe `ALREADY_BOOKED` e não diz se foi a nossa tentativa que deu certo. Precisamos de reconciliação. |
| **Entrada vinda da voz**       | Cidade e estado são falados pelo carrier: normalizar o estado para a sigla, mapear o equipamento e sanitizar `|` e quebras de linha.                           |




### 1.1 O que a sondagem real confirmou ou contradisse


| #   | Manual / suposição                                      | Servidor real (2026-10-20)                                                                                                          | Impacto                                                                                               |
| --- | ------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| 1   | O token pode não ter `MAX_BUY`                          | ✅ **O nosso token recebe** `MAX_BUY` (`LD01012`: `RATE` 755, `MAX_BUY` 891)                                                         | Capability `has_max_buy = true`; segue valendo o fallback para o caso de o token mudar                |
| 2   | `MAX_BUY < RATE` nas transcrições (1950 < 2150…)        | Nas **50 cargas** do servidor, `MAX_BUY` é **sempre maior** que `RATE`: razão 1,05 a 1,24 (mediana 1,15); nenhuma abaixo de 1       | Regra real: o teto fica 5–24% **acima** do loadboard; as transcrições do manual são exemplos atípicos |
| 3   | `LOAD_ID` = `LD` + 10 dígitos                           | `LD01012` (`LD` **+ 5 dígitos**), em campo de 12 com espaços à direita                                                              | Validar `^LD\d+$` com `rstrip()`, **sem fixar 10 dígitos**                                            |
| 4   | Números com zeros à esquerda (`0002150`)                | **Sem zeros**: `755`, `285`, `26713`, `6`, alinhados à esquerda com espaços (`RATE:755` )                                           | `int(v.strip())`; nunca confiar em zero-padding                                                       |
| 5   | Filtro `EQTYPE` inválido → nenhum erro, nenhum registro | `ERR|CODE:MISSING_FIELD|MSG:Invalid EQTYPE`                                                                                         | O TMS **avisa**; ainda assim manter a allowlist no adapter                                            |
| 6   | `LOAD_GET` de id inexistente → `UNKNOWN_LOAD`           | `ERR|CODE:NOT_FOUND|MSG:Load not found` (o `LOAD_BOOK` continua com `UNKNOWN_LOAD`)                                                 | Tratar `NOT_FOUND` e `UNKNOWN_LOAD` como o mesmo caso                                                 |
| 7   | `INVALID_RATE` com regra não documentada                | `AGREED_RATE must be a positive integer`; **checado antes** de `UNKNOWN_LOAD`                                                       | Zero/negativo/não inteiro caem aqui                                                                   |
| 8   | Falhas injetadas                                        | ✅ Presentes: respostas **parciais** (sem `END`, uma delas após 8 s) e **malformadas**; `DEBUG_ECHO` e erros de validação não falham | Retry com conexão nova é obrigatório                                                                  |
| 9   | Código do flatbed desconhecido                          | ✅ `EQTYPE:FLATBED`                                                                                                                  | Allowlist: `DRY_VAN`, `REEFER`, `FLATBED` (outros ainda a levantar)                                   |


| 10 | Volume e `EQTYPE` | Varrimento dos 50 estados + DC: **50 cargas, todas** `OPEN`. `EQTYPE`: `FLATBED` 13, `STEP_DECK` 13, `POWER_ONLY` 13, `DRY_VAN` 8, `REEFER` 3 | Allowlist completa: `DRY_VAN`, `REEFER`, `FLATBED`, `STEP_DECK`, `POWER_ONLY` |
| 11 | Filtro de data desconhecido | `PICKUP_DATE:YYYYMMDD` é reconhecido (com data+hora devolve `MISSING_FIELD: Invalid PICKUP_DATE`). `PICKUP_DT` e `DATE` não dão erro, então podem estar sendo ignorados | Usar `PICKUP_DATE` com 8 dígitos |
| 12 | Teto de `MAX_RESULTS` | Não determinado: a base tem poucas cargas (GA = 3) | Pedir ≤ 50 |
| 13 | Taxa de falha | **200** `LOAD_GET` **sem retry: 168 ok (84%), 18 timeout, 8 parciais, 6 malformadas**; p50 577 ms, p95 5 s (os timeouts) | Retry de 3 tentativas dá ≈ 99,6% de sucesso (0,16³); o orçamento de ~6 s precisa de timeout de leitura menor (ver 7) |
| 14 | Filtro de cidade | Prefixo `Atla` encontrou Atlanta | Match tolerante por prefixo/substring |

| 15 | `LOAD_BOOK` com sucesso | **Reserva real feita** em `LD01031` (POWER_ONLY, GA→OH, `RATE` 1125, `MAX_BUY` 1234) com `AGREED_RATE` 1012 (90% do `RATE`, abaixo do teto) e `MC_NUM` 123456: resposta `STATUS:PENDING` (**não `BOOKED`**), `BOOKING_REF:LT9RTA3KPYMF2YCZ`, `TIMESTAMP:20261020205503` | O estado de sucesso do booking é `PENDING`; tratar `PENDING` e `BOOKED` como reservado |
| 16 | `ALREADY_BOOKED` | 2º `LOAD_BOOK` na mesma carga: `ERR\|CODE:ALREADY_BOOKED\|MSG:Already booked by this token` | A mensagem diz que foi **este token**: dá para reconciliar um retry após `UNKNOWN` |
| 17 | Visibilidade após reservar | A carga **continua na busca** com `STATUS:PENDING` (`LD01031` na busca de GA) | O filtro `STATUS == OPEN` no adapter é obrigatório, não opcional |

**Carga consumida nos testes:** `LD01031`. Não usá-la na demo.

Resultado bruto: `discovery/results/tms_sweep.json`. Código: `adapter/tms_client.py` (cliente com validação, retry e guard-rail do `MAX_BUY`) e `tests/test_tms_client.py`.

---



## 2. Transporte e ciclo de conexão


| Propriedade             | Valor                                                                                                                      |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| Transporte              | TCP (host:port no candidate portal)                                                                                        |
| Encoding                | ASCII                                                                                                                      |
| Terminador de linha     | `\r\n`                                                                                                                     |
| Tamanho máximo de frame | 4096 bytes (incluindo o terminador)                                                                                        |
| Idle timeout            | 30 s (o servidor fecha a conexão)                                                                                          |
| Ciclo                   | **Uma requisição por conexão.** O servidor fecha depois de responder. Reaproveitar a conexão tem comportamento indefinido. |


**Regras do cliente:**

1. Abrir uma conexão nova a cada comando. **Não usar pool de conexões persistentes.** O que limita é a concorrência, via semáforo.
2. Timeouts **nossos**, bem menores que os 30 s do servidor (o carrier está na linha):
  - connect: ~2 s
  - leitura total da resposta: ~5 s para `LOAD_QUERY`/`LOAD_GET` e ~8 s para `LOAD_BOOK`
3. **Fechar a conexão do nosso lado assim que ler** `END\r\n` **ou uma linha** `ERR|…`, sem esperar o servidor (protege contra *delayed termination*).
4. Ler em buffer até `\r\n`, com limite de 4096 bytes por linha; passou disso, a resposta é malformada.
5. Encoding estrito: bytes fora de ASCII tornam a resposta malformada.

---



## 3. Formato das mensagens



### 3.1 Requisição

```
CMD:<command>|AUTH:<token>|<FIELD>:<VALUE>|...\r\n
```

- `CMD` vem **primeiro** e `AUTH` é obrigatório em **toda** requisição.
- Nomes de campo em MAIÚSCULAS.
- Valores **não podem** conter `|`, `\r` ou `\n`. O encoder precisa **rejeitar ou sanitizar** (a entrada vem da voz, e isso evita injeção de campos como `Miami|MAX_RESULTS:999`).
- Campos desconhecidos são aceitos e descartados, ou seja, **um nome de filtro errado não gera erro**: o filtro só deixa de ser aplicado. Isso é perigoso e exige validar o encoder com o `DEBUG_ECHO` (seção 7.4).



### 3.2 Resposta de sucesso

```
<FIELD>:<VALUE>|<FIELD>:<VALUE>|...\r\n     (zero ou mais registros)
END\r\n
```



### 3.3 Resposta de erro

```
ERR|CODE:<code>|MSG:<msg>\r\n
```



### 3.4 Regras de parse

1. **Parsear por nome do campo, não por posição.** O manual avisa que a ordem é estável dentro de um build, mas pode mudar entre builds.
2. Dividir cada par no **primeiro** `:` (`k, v = par.split(":", 1)`), porque um valor de texto livre pode conter `:`.
3. Valores de largura fixa vêm com espaços à direita: fazer `rstrip()` no valor. ✅ Observado: isso vale também para os **números** (`RATE:755`     ), que vêm alinhados à esquerda e **sem zeros à esquerda**; converter com `int(v.strip())`. Um `NOTES` em branco vira `""`, o que é esperado.
4. Ignorar campos desconhecidos (compatibilidade futura), mas **exigir os obrigatórios** de cada comando.
5. **Validar cada campo** por tipo e largura (seção 4). Valor maior que a largura declarada é sinal de resposta malformada.
6. **Só considerar a resposta válida se terminar em** `END`**.** Sem `END`, ela está truncada e é descartada **inteira**, mesmo que algumas linhas pareçam boas.

---



## 4. Dicionário de campos (larguras contadas nas transcrições)


| Campo TMS     | Largura              | Tipo / formato                                                              | Exemplo                            | Campo do case         |
| ------------- | -------------------- | --------------------------------------------------------------------------- | ---------------------------------- | --------------------- |
| `LOAD_ID`     | 12                   | `LD` + dígitos, padding à direita. ✅ Observado: `LD` + **5** dígitos        | `LD01012` (manual: `LD0000045821`) | `load_id`             |
| `ORIG_CITY`   | 30                   | texto, padding à direita                                                    | `Atlanta`                          | `origin`              |
| `ORIG_STATE`  | 2                    | sigla UF dos EUA                                                            | `GA`                               | `origin`              |
| `ORIG_ZIP`    | 5                    | 5 dígitos (pode começar com 0)                                              | `30303`                            | `origin`              |
| `DEST_CITY`   | 30                   | texto                                                                       | `Dallas`                           | `destination`         |
| `DEST_STATE`  | 2                    | sigla                                                                       | `TX`                               | `destination`         |
| `DEST_ZIP`    | 5                    | 5 dígitos, **tratar como string** (`07102`)                                 | `75201`                            | `destination`         |
| `PICKUP_DT`   | 14                   | `YYYYMMDDHHMMSS`                                                            | `20260512080000`                   | `pickup_datetime`     |
| `DELIVERY_DT` | 14                   | `YYYYMMDDHHMMSS` (só no `LOAD_GET`)                                         | `20260513170000`                   | `delivery_datetime`   |
| `EQTYPE`      | 10                   | enum em maiúsculas. ✅ Visto: `FLATBED`                                      | `DRY_VAN`, `REEFER`, `FLATBED`     | `equipment_type`      |
| `RATE`        | 7                    | inteiro em USD. ✅ Observado: **sem zeros**, alinhado à esquerda com espaços | `755` (manual: `0002150`)          | `loadboard_rate`      |
| `MAX_BUY`     | 7                    | inteiro em USD, **condicional** (✅ presente no nosso token)                 | `891` (manual: `0001950`)          | `max_rate` 🔒         |
| `WEIGHT`      | 7                    | inteiro (lbs), sem zeros no real                                            | `26713`                            | `weight`              |
| `COMMODITY`   | 32                   | texto livre                                                                 | `PALLETIZED CONSUMER GOODS`        | `commodity_type`      |
| `PIECES`      | 6                    | inteiro, sem zeros no real                                                  | `6`                                | `num_of_pieces`       |
| `MILES`       | 6                    | inteiro, sem zeros no real                                                  | `285`                              | `miles`               |
| `DIMS`        | 35                   | texto livre                                                                 | `48X40 STD GMA PALLETS`            | `dimensions`          |
| `NOTES`       | 114                  | texto livre, pode vir vazio                                                 | `Drop trailer at destination…`     | `notes`               |
| `STATUS`      | 8 (no GET e no BOOK) | enum                                                                        | `OPEN`, `BOOKED`                   | —                     |
| `BOOKING_REF` | 16                   | opaco, **não parsear**                                                      | `BR00000000091277`                 | referência do booking |
| `TIMESTAMP`   | 14                   | `YYYYMMDDHHMMSS`, **UTC**                                                   | `20260504193122`                   | data/hora da reserva  |


**Observações:**

- `STATUS` aparece sem padding (4 caracteres) no fim da linha do `LOAD_QUERY` e com padding (8) no `LOAD_GET`/`LOAD_BOOK`. Pode ser trim do último campo ou artefato do manual. **⚠️ A validar**; aceitar de 1 a 8.
- `DIMS` e `COMMODITY` são texto livre, digitado por operador: **não esperar vocabulário controlado** (o case pede L x W x H em polegadas, mas o TMS traz `48X40 STD GMA PALLETS`).
- ✅ Observado: `PICKUP_DT:20261026130300` e `DELIVERY_DT:20261028150300` seguem `YYYYMMDDHHMMSS`; `DIMS` real é `49ft x 8ft x 9ft`, `COMMODITY` é `Machinery` e `NOTES` veio vazio (espaços).
- Fuso de `PICKUP_DT`/`DELIVERY_DT` **não documentado** (só o `TIMESTAMP` do booking é declarado UTC). **⚠️ A validar**; provavelmente é hora local da origem.
- `RATE` é **inteiro em dólares**. Se o `AGREED_RATE` aceita centavos, **⚠️ A validar**; por padrão, mandar inteiro.

---



## 5. Comandos



### 5.1 `LOAD_QUERY`: buscar cargas abertas

**Requisição:** pelo menos **um filtro** além de `CMD`/`AUTH` (sem filtro → `MISSING_FIELD`).


| Filtro                      | Observado | Regra                                                                                                                   |
| --------------------------- | --------- | ----------------------------------------------------------------------------------------------------------------------- |
| `ORIG_CITY` / `DEST_CITY`   | ✅         | match **tolerante** (`Miami` também trouxe `Miami Gardens`)                                                             |
| `ORIG_STATE` / `DEST_STATE` | ✅         | match **estrito**: só sigla de 2 letras                                                                                 |
| `ORIG_ZIP` / `DEST_ZIP`     | inferido  | match estrito. **⚠️ A validar** o nome do filtro                                                                        |
| `EQTYPE`                    | ✅         | valor fora do conjunto → ✅ Observado: `ERR|CODE:MISSING_FIELD|MSG:Invalid EQTYPE` (o manual dizia que não haveria erro) |
| data de pickup              | ❓         | nome do campo **não documentado** (testar `PICKUP_DT`, `PICKUP_DATE`). **⚠️ A validar**                                 |
| `MAX_RESULTS`               | ✅         | o servidor aplica um teto próprio, não divulgado                                                                        |


```
> CMD:LOAD_QUERY|AUTH:<token>|ORIG_STATE:GA|DEST_STATE:TX|EQTYPE:DRY_VAN|MAX_RESULTS:5
< LOAD_ID:LD0000045821|ORIG_CITY:Atlanta                       |ORIG_STATE:GA|ORIG_ZIP:30303|DEST_CITY:Dallas                        |DEST_STATE:TX|DEST_ZIP:75201|PICKUP_DT:20260512080000|EQTYPE:DRY_VAN   |RATE:0002150|MILES:000785|STATUS:OPEN
< END
```

**Retorno por registro:** `LOAD_ID, ORIG_*, DEST_*, PICKUP_DT, EQTYPE, RATE, MILES, STATUS`. **Não traz** `DELIVERY_DT`, `WEIGHT`, `COMMODITY`, `NOTES` nem `MAX_BUY`, então é preciso um `LOAD_GET` antes do pitch.

**Resultado vazio** é só `END`. Atenção: uma resposta truncada **sem** `END` não é "vazio", é falha.

**Implicações para o processo:**

- **Normalizar a fala do carrier** antes da busca:
  - Estado: "Georgia" → `GA` (tabela dos 50 estados mais DC). Como o match de estado é estrito, um erro aqui retorna vazio em silêncio.
  - Equipamento: "dry van"/"van" → `DRY_VAN`, "reefer"/"refrigerado" → `REEFER`, "flatbed" → **⚠️ A validar** o código (`FLATBED`?).
- **Descobrir o conjunto de** `EQTYPE` com buscas amplas no setup e manter uma allowlist. Rejeitar no adapter um valor fora dela (o TMS real devolve `Invalid EQTYPE`, mas com código `MISSING_FIELD`, que não deve ser confundido com bug do encoder).
- Estratégia de busca em cascata: (cidade+estado de origem, destino, equipamento) → relaxar o destino → só estado de origem e equipamento. Mostrar ao agente no máximo 1 a 3 opções.



### 5.2 `LOAD_GET`: detalhe completo de uma carga

```
> CMD:LOAD_GET|AUTH:<token>|LOAD_ID:LD0000045821
< LOAD_ID:LD0000045821|...|PICKUP_DT:20260512080000|DELIVERY_DT:20260513170000|EQTYPE:DRY_VAN   |RATE:0002150|WEIGHT:0042000|COMMODITY:PALLETIZED CONSUMER GOODS       |PIECES:000026|MILES:000785|DIMS:48X40 STD GMA PALLETS              |NOTES:Drop trailer at destination. Appt required.          ...|STATUS:OPEN    |MAX_BUY:0001950
< END
```

- Erro: id inexistente → ✅ Observado `NOT_FOUND` / `Load not found` (o manual dizia `UNKNOWN_LOAD`); aceitar os dois.
- **Retorna a carga em qualquer** `STATUS`, inclusive já reservada ou com pickup vencido. O adapter precisa **filtrar** `STATUS == OPEN` **e** `PICKUP_DT` **no futuro** antes de oferecer a carga.
- `MAX_BUY` **só aparece para tokens com permissão.** Na ausência, o campo **some** do registro (não vem vazio).
  - ✅ **Observado:** o nosso token **recebe** `MAX_BUY` (`LD01012`: 891).
  - **Detecção:** no startup, fazer um `LOAD_GET` numa carga conhecida e registrar a capability `has_max_buy`.
  - **Sem** `MAX_BUY`**, o sistema não pode negociar** acima de nada com segurança. Fallback a definir com o cliente: aceitar só até `RATE`, ou um teto calculado (por exemplo, `RATE × fator`) vindo de configuração. **Nunca inventar o teto no LLM.**

> ⚠️ **Ponto de negócio (relação** `MAX_BUY` **×** `RATE` **não é uniforme):** nas três transcrições do manual, `MAX_BUY < RATE` (1950 < 2150, 3080 < 3420, 2065 < 2280), mas na carga real observada `MAX_BUY > RATE` (891 > 755). Ou seja, **não dá para assumir nenhuma das duas relações**. O Negotiation Engine tem que comparar sempre com o `MAX_BUY` da própria carga, nunca ancorar no `RATE` e tratar o caso `MAX_BUY < RATE` (abrir abaixo do teto e negociar até ele; pedido de "rate do loadboard" vira contraproposta). Falta uma amostra maior para saber qual relação é a regra (seção 9, item 3).



### 5.3 `LOAD_BOOK`: confirmar a reserva

```
> CMD:LOAD_BOOK|AUTH:<token>|LOAD_ID:LD0000045821|MC_NUM:872144|AGREED_RATE:2200
< LOAD_ID:LD0000045821|BOOKING_REF:BR00000000091277|STATUS:BOOKED  |TIMESTAMP:20260504193122
< END
```


| Campo         | Obrigatório | Formato                                             |
| ------------- | ----------- | --------------------------------------------------- |
| `LOAD_ID`     | sim         | `LD` + 10 dígitos                                   |
| `MC_NUM`      | sim         | só dígitos, sem prefixo `MC`                        |
| `AGREED_RATE` | sim         | inteiro em USD, **sem** zeros à esquerda no exemplo |



| Erro             | Significado                                                                                                                                                                                                             | Ação                                                  |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| `ALREADY_BOOKED` | carga indisponível **para este token**                                                                                                                                                                                  | ver a reconciliação abaixo                            |
| `INVALID_RATE`   | rate rejeitado. ✅ Observado: `AGREED_RATE must be a positive integer` (testado com `0`), checado **antes** de `UNKNOWN_LOAD`. O manual avisa que "cobre mais que os casos óbvios", então a faixa máxima segue a validar | não repetir com o mesmo valor; logar e acionar humano |
| `UNKNOWN_LOAD`   | id inexistente                                                                                                                                                                                                          | erro de fluxo; logar                                  |
| `MISSING_FIELD`  | faltou campo                                                                                                                                                                                                            | bug do encoder; alerta                                |


**Regras importantes:**

- **A reserva é amarrada ao token, não ao carrier.** O mesmo token atende todas as ligações, então dois carriers fechando a mesma carga ao mesmo tempo resultam num sucesso e num `ALREADY_BOOKED`, sem o TMS dizer qual. ⇒ **Lock por** `LOAD_ID` **no nosso lado** (reserva tentativa local com TTL durante a negociação) e serialização do `LOAD_BOOK`.
- `ALREADY_BOOKED` **é monotônico por token:** uma vez visto, nunca mais muda para aquela carga com aquele token.
- **O TMS não aplica o teto da brokerage.** Ele aceitou 2200 numa carga com `MAX_BUY` 1950. ⇒ **Validar** `AGREED_RATE ≤ MAX_BUY` **no adapter antes de enviar** (um último guard-rail, independente do Negotiation Engine).
- `BOOKING_REF` é opaco: guardar como string no Twin e nunca parsear.
- Faixa aceita de `AGREED_RATE`: **⚠️ A validar** por observação (mínimo, máximo, relação com `RATE`).

**Máquina de estados do booking (idempotência sob falha):**

```
              ┌──────────── sucesso (registro + END) ───────────► BOOKED (salva BOOKING_REF)
 PENDING ─────┤
 (grava no    ├──── ERR ALREADY_BOOKED (1ª tentativa) ──────────► LOST (outro fechou)
  Twin antes  │
  de enviar)  └──── timeout / truncada / malformada ───► UNKNOWN
                                                           │
                                         retry LOAD_BOOK (mesmo rate)
                                                           │
                      ┌────────────── sucesso ─────────────┴──► BOOKED
                      └──── ALREADY_BOOKED ──► LOAD_GET: STATUS==BOOKED?
                                                  ├─ sim → BOOKED_UNCONFIRMED (sem BOOKING_REF;
                                                  │        provável que tenha sido a nossa tentativa;
                                                  │        sinalizar para o senior rep confirmar)
                                                  └─ não/erro → NEEDS_HUMAN
```

- O estado `PENDING` é gravado **antes** do envio, com `call_id`, `load_id`, `mc`, `rate` e o horário.
- Como o lock local garante que só a nossa ligação tentou aquela carga, um `ALREADY_BOOKED` **depois** de uma tentativa `UNKNOWN` significa, com alta probabilidade, que a nossa reserva entrou. Mesmo assim, ela fica marcada como não confirmada para revisão humana.
- No máximo 2 tentativas de `LOAD_BOOK` por chamada. O carrier ouve algo como "Sua carga está reservada, o senior rep vai confirmar os detalhes", sem expor a falha técnica.



### 5.4 `DEBUG_ECHO`: diagnóstico

```
> CMD:DEBUG_ECHO|AUTH:<token>|MSG:HELLO
< ECHO|AUTH:OK|FIELDS_PARSED:3|MSG:HELLO
< END
```

- **Não passa pela injeção de falhas.** Serve para validar o **token, a conectividade e o encoder**, **não** para medir a saúde operacional.
- `FIELDS_PARSED` conta todos os pares aceitos, incluindo `CMD` e `AUTH`. Use para:
  - **Teste de contrato do encoder:** mandar N campos e conferir `FIELDS_PARSED == N`.
  - Detectar sanitização falha: um valor com `|` escondido aumentaria a contagem.
- Healthcheck do container: `DEBUG_ECHO` informa "alcançável e autenticado". A saúde real vem das métricas de sucesso dos comandos operacionais (circuit breaker).

---



## 6. Erros conhecidos


| Código           | Origem   | Retry? | Observação                                                                    |
| ---------------- | -------- | ------ | ----------------------------------------------------------------------------- |
| `AUTH_FAILED`    | qualquer | ❌      | o servidor fecha a conexão. Token inválido ou revogado ⇒ alerta crítico       |
| `UNKNOWN_CMD`    | qualquer | ❌      | bug                                                                           |
| `MISSING_FIELD`  | qualquer | ❌      | bug do encoder, consulta sem filtro, ou ✅ `Invalid EQTYPE` (olhar o `MSG`)    |
| `UNKNOWN_LOAD`   | BOOK     | ❌      |                                                                               |
| `NOT_FOUND`      | GET      | ❌      | ✅ Observado (`Load not found`); equivale a `UNKNOWN_LOAD`                     |
| `ALREADY_BOOKED` | BOOK     | ❌      | monotônico; ver a reconciliação                                               |
| `INVALID_RATE`   | BOOK     | ❌      | regra não documentada                                                         |
| `MALFORMED`      | qualquer | ⚠️ 1x  | provavelmente o servidor não entendeu a requisição; revisar o encoder         |
| `SERVER_ERROR`   | qualquer | ✅      | transitório                                                                   |
| *(desconhecido)* | qualquer | ❌      | a lista **não é exaustiva**: tratar como erro genérico e logar o código bruto |


---



## 7. Falhas injetadas e como o adapter reage

As falhas **não são sinalizadas**: *"The operator's contract is with the wire, not with the server."*


| Categoria               | Como aparece no fio                                               | Detecção                                | Reação                                                       |
| ----------------------- | ----------------------------------------------------------------- | --------------------------------------- | ------------------------------------------------------------ |
| **Timeout**             | conexão aceita, nenhum byte de resposta                           | o nosso read timeout estoura            | fechar; retry (QUERY/GET) ou `UNKNOWN` (BOOK)                |
| **Resposta parcial**    | registros sem `END` e EOF; a última linha pode estar cortada      | EOF antes do `END`                      | **descartar tudo**; retry                                    |
| **Malformada**          | delimitadores extras, linha sem `\r\n`, valor maior que a largura | validação de schema por campo (seção 4) | descartar; retry                                             |
| **Terminação atrasada** | resposta completa, mas a conexão não fecha                        | já temos o `END`                        | **fechar do nosso lado** ao ler `END`/`ERR`: resposta válida |
| *Novas categorias*      | —                                                                 | qualquer violação de schema             | tratar como malformada                                       |


**Política de resiliência:**

- **Retry com backoff e jitter**: 3 tentativas em `LOAD_QUERY`/`LOAD_GET` (por exemplo, 200 ms, 500 ms e 1 s), sempre com conexão nova. O total precisa caber no orçamento de latência da ligação (~6 s), enquanto o agente fala algo como "um segundo, deixa eu puxar aqui".
- `LOAD_BOOK` segue a máquina de estados da seção 5.3; nunca fazer retry cego.
- **Circuit breaker** por comando, a partir da taxa de falha numa janela: aberto, responde rápido com `TMS_UNAVAILABLE` e o agente oferece callback.
- **Cache** curto (30–60 s) de `LOAD_QUERY` e `LOAD_GET`, só para cargas `OPEN`. **Invalidar a carga** depois de qualquer `LOAD_BOOK`.
- **Limitar a concorrência** (semáforo de ~10 conexões simultâneas) para não piorar o servidor que já é instável.
- **Observabilidade:** contadores por categoria de falha, latência p50/p95 e linha bruta logada **com o token mascarado**. Isso alimenta o KPI de "chamadas degradadas por falha do TMS".

---



## 8. Interface que o adapter expõe ao workflow (HTTP)

O agente HappyRobot nunca fala TCP: ele chama a Integration API, que esconde o protocolo, as falhas e o `MAX_BUY`.


**Implementado** em `adapter/server.py` e `adapter/service.py` (stdlib; `python3 -m adapter.server`, porta `PORT` ou 8080, só em `127.0.0.1`). **Toda rota exige** `Authorization: Bearer <TMS_TOKEN>` (401 sem ele).

| Endpoint | Comando TMS | Retorno ao agente (sem `MAX_BUY`) |
|---|---|---|
| `GET /loads/search?origin_city&origin_state&dest_state&equipment&pickup_date` | `LOAD_QUERY` (cascata) e `LOAD_GET` | até 3 cargas reserváveis (`OPEN` e pickup futuro), `relaxed: true` se os filtros foram relaxados. Aceita estado por extenso e equipamento em linguagem natural |
| `GET /loads/{load_id}` | `LOAD_GET` | detalhe para o pitch; 404 se a carga não for reservável |
| `POST /negotiation/evaluate {load_id, ask}` | — (usa o `MAX_BUY` interno) | `accept` se `ask ≤ MAX_BUY`; senão `counter` com `rate = MAX_BUY`. Não negocia abaixo do teto: a margem já está embutida |
| `POST /loads/{load_id}/book {mc_num, agreed_rate, call_id}` | `LOAD_BOOK` | `booked` / `booked_unconfirmed` / `unavailable` / `error` (`rate_above_ceiling`, `needs_human`, `invalid_rate`…). `agreed_rate` é o valor aceito pelo carrier |
| `GET /health` | `DEBUG_ECHO` | status do adapter, do TMS e do circuit breaker |

Decisões: datas devolvidas em **UTC** com sufixo `Z` (fuso real de `PICKUP_DT` ainda não confirmado, assumido UTC); lock por `LOAD_ID` e estado do booking **em memória** (instância única; trocar pelo Twin); circuit breaker abre após 6 falhas seguidas por 20 s (503 `TMS_UNAVAILABLE`).

Testes: `tests/test_service.py` (12, sem rede), `tests/test_tms_client.py` (8) e `tests/test_tms_live.py` (4, só leitura contra o TMS real).

Modelo interno (antes de projetar para o agente):

```json
{
  "load_id": "LD0000045821",
  "origin": {"city": "Atlanta", "state": "GA", "zip": "30303"},
  "destination": {"city": "Dallas", "state": "TX", "zip": "75201"},
  "pickup_datetime": "2026-05-12T08:00:00",
  "delivery_datetime": "2026-05-13T17:00:00",
  "equipment_type": "DRY_VAN",
  "loadboard_rate": 2150,
  "max_rate": 1950,
  "weight": 42000, "commodity_type": "PALLETIZED CONSUMER GOODS",
  "num_of_pieces": 26, "miles": 785,
  "dimensions": "48X40 STD GMA PALLETS",
  "notes": "Drop trailer at destination. Appt required.",
  "status": "OPEN"
}
```

> `max_rate` só existe dentro do backend. O serializador da resposta ao agente usa uma **allowlist de campos**, e não uma blocklist, para o teto não vazar por engano.

---



## 9. Sondagens

**Feitas em 2026-10-20** com `discovery/run_tms_probes.py` (lê `TMS_HOST`, `TMS_PORT` e `TMS_TOKEN` do `.env`, mascara o token e só testa `LOAD_BOOK` com id inexistente): itens 1 (parcial: 3 e 6 campos), 2, parte do 3 (`FLATBED`), 10 (`:` no valor). Resultado bruto: `discovery/results/tms_results.json`.

**Feitas em 2026-10-20 (varrimento,** `discovery/run_tms_sweep.py`**):** itens 3 (50 estados), 4 (`PICKUP_DATE`), 5 (ZIP funciona), 6 (inconclusivo) e 8 (200 `LOAD_GET`). O validador do script de sondagem foi corrigido (`LD` + qualquer número de dígitos).

**Falta:** faixa de `AGREED_RATE` aceita (só se viu 1012 aceito e 0 recusado; mínimo e máximo seguem sem teste), fuso de `PICKUP_DT` e padding de `STATUS` (9), acento (10) e `DEBUG_ECHO` com 20 campos (1).

Roteiro completo (respostas brutas como *fixtures*, sem o token):

1. `DEBUG_ECHO` com 3, 6 e 20 campos para conferir `FIELDS_PARSED` (contrato do encoder).
2. `LOAD_GET` numa carga conhecida para ver **se o nosso token recebe** `MAX_BUY` (capability).
3. `LOAD_QUERY` amplo por estado (os 50 estados) para levantar o **conjunto real de** `EQTYPE` (inclusive o código de flatbed) e o volume de cargas.
4. Descobrir o **nome do filtro de data** (`PICKUP_DT`, `PICKUP_DATE`, `DATE`…), comparando os resultados com e sem ele.
5. Confirmar os filtros `ORIG_ZIP`/`DEST_ZIP`.
6. Descobrir o **teto de** `MAX_RESULTS` (pedir 1000 e contar).
7. Testar a **faixa de** `AGREED_RATE` aceita (centavos? acima de `RATE`? acima de `MAX_BUY`? valores muito baixos?), usando cargas descartáveis, já que cada reserva consome a carga para o token.
8. Medir a **taxa e o tipo de falha**: 200 chamadas de `LOAD_GET`, classificando timeout, parcial, malformada e atrasada, mais as latências. Isso calibra timeouts, retries e o breaker.
9. Confirmar o fuso de `PICKUP_DT` e o padding de `STATUS` no `LOAD_QUERY`.
10. Testar uma requisição com valor contendo `:` e caracteres não-ASCII (nomes de cidade com acento) para ver o comportamento do servidor.

> ⚠️ Cada `LOAD_BOOK` bem-sucedido torna a carga `ALREADY_BOOKED` **para sempre** naquele token. Reservar só o mínimo durante os testes e não gastar as cargas da demo.

---



## 10. Testes do adapter (simulador local)

Criar um **servidor TCP fake** que reproduz o protocolo e injeta as 4 categorias de falha com probabilidade configurável. Casos:

- Parse dos registros das transcrições oficiais (golden files), com `NOTES` vazio e cidade com espaço (`Miami Gardens`).
- Campo em ordem diferente, campo desconhecido extra e valor contendo `:`.
- Resposta sem `END`, linha cortada no meio, `|` duplicado, valor maior que a largura, linha acima de 4096 bytes.
- Servidor que não responde e servidor que não fecha a conexão depois do `END`.
- `ERR` com código desconhecido.
- `LOAD_BOOK` com timeout seguido de `ALREADY_BOOKED` no retry (reconciliação).
- Duas ligações reservando a mesma carga ao mesmo tempo (lock).
- Entrada da voz com `|`, `\r\n`, estado por extenso e equipamento fora da allowlist.
- `AGREED_RATE > MAX_BUY` bloqueado no adapter, sem nem chegar ao TMS.

---



## 11. Segurança do token

- O token é **credencial bearer** e vale para a instância inteira (não é restrito por MC).
- Hoje ele está no `.env` do projeto como `TMS_TOKEN` (junto com `TMS_HOST` e `TMS_PORT`). O `.env.example` ainda **não lista** `TMS_TOKEN`: acrescentar a chave, sem valor. A pasta não é um repositório git; ao criar um, colocar `.env` no `.gitignore` antes do primeiro commit.
- Fica em variável de ambiente ou no secret manager; **nunca** no repositório, nos logs, nos transcripts ou no vídeo.
- Mascarar `AUTH:` em todo log de linha bruta (`AUTH:t-9c3a…` → `AUTH:***`).
- O token pode ser revogado sem aviso: `AUTH_FAILED` gera um alerta imediato no App e o circuit breaker abre.

---



## 12. Perguntas em aberto

1. ~~O nosso token terá a permissão de~~ `MAX_BUY`~~?~~ **Resolvida:** sim, o token recebe `MAX_BUY`. Resta decidir o fallback caso ele seja revogado ou trocado.
2. `MAX_BUY` ~~×~~ `RATE` **Resolvida pela amostra:** `MAX_BUY` = 1,05 a 1,24 × `RATE` nas 50 cargas. Resta confirmar com o cliente se isso é intencional e de qual valor a negociação parte.
3. Qual o fuso de `PICKUP_DT`/`DELIVERY_DT`?
4. Qual a regra real do `INVALID_RATE`?
5. ~~Códigos de~~ `EQTYPE` **Resolvida:** `DRY_VAN`, `REEFER`, `FLATBED`, `STEP_DECK`, `POWER_ONLY`.
6. Existe forma de **liberar** uma reserva no TMS? O protocolo não tem comando de cancelamento, então um handoff que não fecha precisa de processo manual.

