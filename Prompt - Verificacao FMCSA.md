# Prompt: módulo de verificação de carrier (FMCSA)

> Prompt para orientar uma IA a implementar `verify_carrier`. Baseado em `Integracao FMCSA.md` (seções 12 e 13), com endpoints e formatos testados em 2026-10-20 com o MC 193047.
>
> **Confirmar com o cliente antes de usar:** (1) `stale_registration` rejeita MCS-150 com mais de 24 meses (limite proposto); (2) `cell_phone` nunca é destino do OTP.
> **Não validado:** rate limit e comportamento sob carga.

---

# PAPEL
Você implementa o módulo `verify_carrier` da Integration API: dado o MC informado por um carrier ao telefone, decide se ele é elegível e devolve o telefone registrado na FMCSA para o envio do OTP. Escreva o código na linguagem/stack do repositório e siga as convenções existentes.

# PRINCÍPIOS (inegociáveis)
1. **Fail-closed:** campo de bloqueio ausente, resposta inesperada, timeout ou erro 5xx → NUNCA aprovar. Retorne `rejected` ou `unverified` com um `reason_code`.
2. **O telefone do OTP vem exclusivamente da FMCSA (census).** Nunca de dado falado na ligação.
3. **O agente de voz nunca recebe o telefone completo.** Só `phone_last4`. O número completo vai apenas para o serviço de OTP.
4. **Nunca logar a `webKey`.** Redija `webKey=...` em qualquer log, erro ou trace.
5. **Não detalhe o motivo da reprovação ao carrier.** O `reason_code` é interno (Twin/App).

# FONTES E ENDPOINTS

## Fonte 1: QCMobile (elegibilidade e identidade). Exige webKey
Base: `https://mobile.fmcsa.dot.gov/qc/services`
Autenticação: `?webKey=<FMCSA_API_KEY>` (variável de ambiente).

| Passo | Endpoint | Para quê | Formato real |
|---|---|---|---|
| 1 | `GET /carriers/docket-number/{mc}` | MC → DOT + dados do carrier | `{"content":[{"_links":{...},"carrier":{...}}],"retrievalDate":"..."}` (**content é lista**) |
| 2 | `GET /carriers/{dot}/authority` | Tipo de autoridade e carga | `{"content":[{"carrierAuthority":{...}}],"retrievalDate":"..."}` |
| 3 | `GET /carriers/{dot}/docket-numbers` | Confirmar que o MC pertence ao DOT | `{"content":[{"docketNumber":193047,"prefix":"MC","dotNumber":74167}, ...]}` |
| 4 (opcional) | `GET /carriers/{dot}/oos` | Detalhe de out-of-service | `{"content":[]}` quando não há ordem de OOS |

`{mc}` = só dígitos, sem prefixo `MC` e sem zeros à esquerda (`0193047` não encontra nada).

Campos do objeto `carrier` a usar (nomes reais; **números podem vir como string ou número**):
`allowedToOperate` ("Y"/"N"; aceite também `allowToOperate`), `statusCode` ("A"=ativo), `commonAuthorityStatus`, `contractAuthorityStatus`, `brokerAuthorityStatus` ("A"/"I"/"N"), `oosDate` (pode vir `null`), `legalName`, `dbaName`, `dotNumber`, `bipdInsuranceOnFile`, `bipdRequiredAmount` (em milhares de USD).
O objeto `carrier` **não traz `mcNumber` nem `telephone`**.

Campos de `carrierAuthority` (passo 2): `authorizedForProperty`, `authorizedForBroker`, `commonAuthorityStatus`, `contractAuthorityStatus`, `brokerAuthorityStatus`, `docketNumber`, `prefix`.
Atenção: `contractAuthorityStatus` já veio **diferente** entre o objeto do carrier ("A") e o `/authority` ("N"). Para a decisão, trate o `/authority` como fonte de verdade para autoridade e registre a divergência no log de auditoria.

## Fonte 2: Census da FMCSA (telefone). Dataset público no Socrata, sem webKey
Base: `https://data.transportation.gov/resource`

| Passo | Endpoint | Para quê |
|---|---|---|
| 5 | `GET /az4n-8mr2.json?dot_number={dot}&$select=dot_number,legal_name,phone,cell_phone,email_address,mcs150_date,status_code` | **Telefone registrado** (principal) |
| 5b (conferência) | `GET /kjg3-diqy.json?dot_number={dot}&$select=telephone,mcs150_date` | Telefone formatado (`(305) 302-3989`); só carriers ativos; atualiza mensalmente |

Consulte **sempre por `dot_number`** (≈1 s). Evite filtrar por `docket1/2/3` (1,3 a 5,7 s). Use o header `X-App-Token` se houver token Socrata configurado.
Resposta real: `[{"dot_number":"74167","legal_name":"...","phone":"3053023989","cell_phone":"6148655914","email_address":"...","mcs150_date":"20260723","status_code":"A"}]`
- `phone` vem **só com dígitos**. `mcs150_date` vem como `YYYYMMDD` (no dataset B: `DD-MON-YY`).
- Lista vazia `[]` = sem registro. HTTP 200, não é erro.

# FLUXO (execute nesta ordem)

```
entrada: call_id, mc_number (texto falado/digitado)

1. NORMALIZAR mc_number
   - remover "MC", espaços, hífens, pontos; remover zeros à esquerda
   - exigir ^[0-9]{1,8}$ ; senão → rejected / mc_invalid_format (o agente pode pedir de novo, no máx. 2x)

2. QCMobile: GET /carriers/docket-number/{mc}
   - content vazio                      → rejected / mc_not_found
   - 5xx ou timeout (após retries)      → unverified / fmcsa_unavailable
   - 401                                → unverified / fmcsa_unavailable + alerta crítico (webKey inválida)
   - content com >1 carrier             → unverified / ambiguous_result
   - extrair dot = carrier.dotNumber, legalName, dbaName

3. Regras de bloqueio sobre o objeto carrier (R1, R2, R5)
   - allowedToOperate (ou allowToOperate) != "Y" ou ausente → rejected / not_allowed_to_operate
   - oosDate presente e não nulo                            → rejected / out_of_service
   - statusCode != "A"                                      → rejected / inactive_registration

4. QCMobile: GET /carriers/{dot}/authority   (R3, R4)
   - autoridade de carrier ativa = commonAuthorityStatus == "A" OU contractAuthorityStatus == "A"
   - nenhuma ativa e brokerAuthorityStatus == "A" → rejected / broker_only_authority
   - nenhuma ativa                                → rejected / no_active_carrier_authority
   - authorizedForProperty != "Y"                 → rejected / no_active_carrier_authority

5. QCMobile: GET /carriers/{dot}/docket-numbers   (R6)
   - não existe item com prefix=="MC" e docketNumber == int(mc) → rejected / mc_mismatch

   (passos 4 e 5 são independentes: execute em paralelo)

6. Census: GET az4n-8mr2 ... ?dot_number={dot}
   - 5xx/timeout (após retries)            → unverified / fmcsa_unavailable
   - lista vazia ou phone vazio            → unverified / no_registered_phone
   - dígitos(phone) com tamanho != 10      → unverified / no_registered_phone
   - mcs150_date mais antigo que 24 meses  → unverified / stale_registration  (política: confirmar com o cliente)
   - senão: phone_e164 = "+1" + dígitos(phone)

7. Resultado aprovado
   - status = approved
   - para o AGENTE: { status, carrier_name: legalName, phone_last4, reason_code: null }
   - para o Twin e o serviço de OTP: dot, campos usados na decisão, retrievalDate,
     mcs150_date, phone_e164 (efêmero, só no serviço de OTP), phone mascarado "***-***-4321" no Twin
```

# RESILIÊNCIA
- Timeout: 3 s para QCMobile; 5 s para o Socrata (latência observada: QCMobile 0,4 a 0,8 s; Socrata 0,7 a 7,5 s conforme o filtro).
- Retry: 2 tentativas com backoff exponencial **apenas** em 5xx, 504 e timeout. Nunca em 4xx.
- Cache por DOT: aprovado 24 h; reprovado 1 h; `unverified` não é cacheado. O telefone entra no cache criptografado e nunca no log.
- Circuit breaker por fonte (QCMobile e Socrata separados). Aberto → `unverified / fmcsa_unavailable`, alerta no App.
- Rate limit: **não validado.** Implemente limite de concorrência configurável (padrão 5) e trate 429 como erro com retry.

# CONTRATO DE SAÍDA
`POST /carriers/verify` com `{ "call_id": "...", "mc_number": "..." }`:
```json
{ "status": "approved|rejected|unverified", "carrier_name": "string|null", "phone_last4": "string|null", "reason_code": "string|null" }
```
`reason_code` ∈ {`mc_invalid_format`, `mc_not_found`, `not_allowed_to_operate`, `out_of_service`, `inactive_registration`, `no_active_carrier_authority`, `broker_only_authority`, `mc_mismatch`, `no_registered_phone`, `stale_registration`, `fmcsa_unavailable`, `ambiguous_result`}.
Tanto `rejected` quanto `unverified` encerram a ligação com a mensagem genérica e vão para a fila humana. Nenhum dos dois libera OTP.

# PARSING DEFENSIVO
- `content` pode ser lista (docket-number, authority, docket-numbers, oos) ou objeto (`/carriers/{dot}`): normalize antes de ler.
- Campos ausentes ou `null` são "sem valor", nunca "falso".
- Coerção tolerante: `"5000"` e `5000` são equivalentes; `dotNumber` e `docketNumber` comparados como inteiros.
- Aceite `allowedToOperate` e `allowToOperate`.

# TESTES OBRIGATÓRIOS
Use fixtures com as respostas reais abaixo e mocks para os erros.
1. **Caminho feliz:** MC `193047` → DOT `74167`, autoridade comum ativa, `phone` `3053023989` → `approved`, `phone_last4 = "3989"`, `phone_e164 = "+13053023989"`.
2. MC `0193047` e `MC-193047` → normalizam para `193047`.
3. MC `99999999` → `mc_not_found`.
4. `allowedToOperate = "N"` → `not_allowed_to_operate`.
5. Só `brokerAuthorityStatus = "A"` → `broker_only_authority`.
6. DOT existente mas sem MC correspondente em `/docket-numbers` → `mc_mismatch`.
7. Census com `[]` → `no_registered_phone`.
8. Timeout/504 no QCMobile e no Socrata → `fmcsa_unavailable` (nunca aprova).
9. Teste de segurança: a `webKey` não aparece em nenhum log nem na mensagem de erro; o agente nunca recebe o telefone completo.

# FORA DE ESCOPO
Não implementar o envio do OTP nem a UI do App. Não usar a busca por nome (`/carriers/name/{name}`) para verificação. Não usar `cell_phone` como destino do OTP: só registrá-lo para auditoria.

# ENTREGA
Código, testes e uma nota curta listando qualquer suposição que você tenha precisado fazer. Antes de implementar, confirme que `FMCSA_API_KEY` está nas variáveis de ambiente e rode uma chamada real com o MC 193047 para validar o parser.
