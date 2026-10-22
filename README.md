# TMS Adapter

A small HTTP/JSON API that sits in front of a legacy TMS (Transportation Management System) which only speaks a custom TCP protocol. It lets a voice agent (or any modern client) search loads, check an offer against the broker's ceiling, and book a load, without knowing anything about the legacy protocol.

```
Voice agent / HTTP client  ──HTTPS+JSON──▶  Adapter (this repo)  ──raw TCP──▶  Legacy TMS
```

The adapter has **no third-party dependencies**: it uses only the Python 3 standard library.

## What is in this repository

| Path | What it is |
|---|---|
| `adapter/server.py` | The HTTP layer. Routing, Bearer-token auth, JSON in/out, error-to-status mapping. Entry point: `python -m adapter.server`. |
| `adapter/service.py` | The business rules. Input normalization, cascading search, caching, offer evaluation, per-load locking and the booking state machine. |
| `adapter/tms_client.py` | The TCP client for the legacy TMS. Encodes requests, validates every response, retries transient failures, masks the token in logs and errors. |
| `tests/` | Automated tests (see [Running the tests](#running-the-tests)). |
| `Dockerfile`, `.dockerignore` | Container image for the adapter. |
| `deploy-azure.sh` | One-command deploy to Azure Container Apps. |
| `deploy-gcp.sh` | One-command deploy to Google Cloud Run. |
| `deploy-aws.sh` | One-command deploy to AWS App Runner. |
| `.env.example` | Template for the configuration variables. |

A good reading order for newcomers: `server.py` (what the API exposes) → `service.py` (what it does) → `tms_client.py` (how it talks to the TMS).

## Configuration

Copy `.env.example` to `.env` and fill in the values. In production, set them as environment variables instead.

| Variable | Required | Description |
|---|---|---|
| `TMS_HOST` | yes | Hostname of the legacy TMS. |
| `TMS_PORT` | yes | TCP port of the legacy TMS. |
| `TMS_TOKEN` | yes | Token used to authenticate against the TMS. **The same value is also the Bearer token that clients must send to this API.** |
| `HOST` | no | Interface to listen on. Default `127.0.0.1` (the Docker image sets `0.0.0.0`). |
| `PORT` | no | Port to listen on. Default `8080`. |

`.env` is git-ignored. Never commit it. (`FMCSA_API_KEY` appears in `.env.example` but is not used by the adapter.)

## Running locally

```bash
cp .env.example .env        # then fill in TMS_HOST, TMS_PORT, TMS_TOKEN
python3 -m adapter.server   # listens on http://127.0.0.1:8080
```

Quick check:

```bash
curl http://127.0.0.1:8080/healthz                                   # no auth, does not touch the TMS
curl -H "Authorization: Bearer $TMS_TOKEN" http://127.0.0.1:8080/health   # also pings the TMS
```

## API reference

Every route except `/healthz` requires `Authorization: Bearer <TMS_TOKEN>`. Responses are always JSON.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz` | Liveness probe for the orchestrator. No auth. Does not call the TMS. |
| `GET` | `/health` | Adapter status, TMS reachability, circuit-breaker state and call metrics. |
| `GET` | `/loads/search` | Find up to 3 bookable loads. Query params: `origin_city`, `origin_state`, `dest_state`, `equipment`, `pickup_date` (`YYYYMMDD`). |
| `GET` | `/loads/{LOAD_ID}` | Details of one load, e.g. `/loads/LD12345`. |
| `POST` | `/negotiation/evaluate` | Body: `{"load_id": "LD12345", "ask": 1800}`. Returns `accept` or `counter`. |
| `POST` | `/loads/{LOAD_ID}/book` | Body: `{"mc_num": "123456", "agreed_rate": 1800, "call_id": "abc"}`. Books the load. |

### Examples

Search:

```bash
curl -H "Authorization: Bearer $TMS_TOKEN" \
  "http://127.0.0.1:8080/loads/search?origin_state=Texas&dest_state=IL&equipment=dry%20van"
```

```json
{ "loads": [ { "load_id": "LD12345", "origin": {"city": "...", "state": "TX", "zip": "..."},
               "destination": {"city": "...", "state": "IL", "zip": "..."},
               "pickup_datetime": "2026-10-23T08:00:00Z", "equipment_type": "DRY_VAN",
               "loadboard_rate": 1700, "max_rate": 1900, "miles": 920, "status": "OPEN" } ],
  "relaxed": false }
```

Evaluate an offer:

```bash
curl -X POST -H "Authorization: Bearer $TMS_TOKEN" -d '{"load_id":"LD12345","ask":2100}' \
  http://127.0.0.1:8080/negotiation/evaluate
# {"decision": "counter", "rate": 1900}
```

Book:

```bash
curl -X POST -H "Authorization: Bearer $TMS_TOKEN" \
  -d '{"mc_num":"MC123456","agreed_rate":1850,"call_id":"call-1"}' \
  http://127.0.0.1:8080/loads/LD12345/book
# {"result": "booked", "booking_ref": "..."}
```

### Error responses

| Status | Body | Meaning |
|---|---|---|
| `400` | `{"error": "bad_request", "detail": "..."}` | Invalid input (unknown state or equipment, bad `load_id`, non-integer rate, invalid JSON). |
| `401` | `{"error": "unauthorized"}` | Missing or wrong Bearer token. |
| `404` | `{"error": "not_found"}` or `{"error": "load_unavailable"}` | Unknown route, unknown load, or a load that is not `OPEN` or already past pickup. |
| `502` | `{"error": "<tms_code>"}` | The TMS rejected the request with an error code. |
| `503` | `{"error": "TMS_UNAVAILABLE"}` | The TMS is down or the circuit breaker is open. |

Booking outcomes are returned with status `200` and a `result` field: `booked`, `booked_unconfirmed`, `unavailable`, or `error` (with a `reason`, for example `rate_above_ceiling` or `needs_human`).

## How it works

**Input normalization.** Clients can send `Texas` or `tx`, `dry van` or `DRY_VAN`; the adapter converts them to the codes the TMS expects. MC numbers accept `MC123456` or `123456`.

**Cascading search.** If an exact search finds nothing, the adapter relaxes one filter at a time: drop the date, then the destination, then the city, and finally search by origin state only. The response includes `relaxed: true` when filters were dropped, so the caller knows the results are approximate. Only loads that are `OPEN` and have a future pickup are returned.

**Offer evaluation.** Each load has a ceiling (`max_rate`, the most the broker will pay). If the carrier's ask is at or below the ceiling the answer is `accept`; otherwise it is `counter` at the ceiling.

**Safe booking.** Booking is the one operation that must never double-book or overpay:
- A lock per load serializes concurrent booking attempts.
- The load is re-read fresh from the TMS right before booking; stale cache is never trusted.
- A final guard rejects any `agreed_rate` above the ceiling (`rate_above_ceiling`).
- If a network failure leaves the outcome unknown, the adapter checks the load's status afterwards and returns `booked_unconfirmed`, or `needs_human` when it cannot tell, rather than guessing.

**Resilience.**
- Transient TMS failures (timeouts, partial or malformed replies) are retried with backoff.
- A circuit breaker opens after repeated failures and answers `503` immediately for a short period, so a sick TMS is not hammered.
- Reads are cached for 45 seconds; the cache is invalidated after a booking.
- At most 10 concurrent TCP connections to the TMS.

**Security.**
- Constant-time token comparison.
- The token is masked in logs and exceptions.
- Access logs omit query strings and headers.
- Public responses use an allowlist of fields.
- The container runs as a non-root user.

**Known limitation.** Booking state is kept in memory. Because of that, the deploy script pins the app to a single replica. Do not scale out without moving that state to a shared store.

## Running the tests

```bash
python3 -m unittest tests/test_service.py tests/test_tms_client.py
```

These are fully offline and run against a fake TMS. They cover normalization, offer evaluation, the booking state machine, and the TCP client's protocol handling.

`tests/test_tms_live.py` is different: it runs read-only requests and error cases against the **real** TMS (it never books anything). It is skipped automatically when `TMS_HOST`, `TMS_PORT` and `TMS_TOKEN` are not set.

## Docker

```bash
docker build -t tms-adapter .
docker run --rm -p 8080:8080 \
  -e TMS_HOST=... -e TMS_PORT=... -e TMS_TOKEN=... tms-adapter
```

The image is based on `python:3.12-slim`, includes a health check on `/healthz`, and contains only the `adapter/` package (no tests, no secrets).

## Deploying to the cloud

The same image runs on any of the three clouds below. Each script reads `TMS_HOST`, `TMS_PORT` and `TMS_TOKEN` from `.env` or the environment, stores the token as a cloud secret (never in the image), pins the app to exactly one instance, and finishes with a smoke test on `/healthz` and `/health`. Pick one.

### Azure Container Apps

Prerequisite: the Azure CLI installed.

**One-line deploy** (from the repository root; logs in to Azure, then builds and deploys with the credentials passed inline):

```bash
az login && TMS_HOST=<host> TMS_PORT=<port> TMS_TOKEN=<token> ./deploy-azure.sh
```

If you are already logged in and have a `.env` with the three variables, it is just:

```bash
./deploy-azure.sh
```

The script reads `TMS_HOST`, `TMS_PORT` and `TMS_TOKEN` from `.env` or the environment, then:
1. Builds the image and creates the app with a public HTTPS endpoint.
2. Stores the token as a Container Apps secret and pins the app to exactly one replica.
3. Prints the public URL and runs a smoke test against `/healthz` and `/health`.

Optional overrides: `APP_NAME` (default `tms-adapter`), `RESOURCE_GROUP` (default `rg-tms-adapter`), `LOCATION` (default `centralus`).

### Google Cloud Run

Prerequisites: the `gcloud` CLI installed and a Google Cloud project with billing enabled.

**One-line deploy:**

```bash
gcloud auth login && GCP_PROJECT=<project-id> TMS_HOST=<host> TMS_PORT=<port> TMS_TOKEN=<token> ./deploy-gcp.sh
```

With a `.env` and a default project (`gcloud config set project <project-id>`), it is just `./deploy-gcp.sh`.

The script enables the required APIs, stores the token in Secret Manager, builds the image with Cloud Build and deploys it to Cloud Run with a public HTTPS URL and CPU always allocated.

Optional overrides: `APP_NAME` (default `tms-adapter`), `GCP_PROJECT` (default: your active gcloud project), `REGION` (default `us-central1`).

### AWS App Runner

Prerequisites: AWS CLI v2 and Docker installed, and AWS credentials configured. The identity needs permissions for ECR, Secrets Manager, IAM roles and App Runner.

**One-line deploy:**

```bash
aws sso login && TMS_HOST=<host> TMS_PORT=<port> TMS_TOKEN=<token> ./deploy-aws.sh
```

(Use `aws configure` once instead of `aws sso login` if you authenticate with access keys.) With a `.env` and active credentials, it is just `./deploy-aws.sh`.

The script builds the image locally for `linux/amd64`, pushes it to a private ECR repository, stores the token in Secrets Manager, creates the two IAM roles App Runner needs (one to pull the image, one to read the secret), and creates the service with a public HTTPS URL. Running it again redeploys the latest image.

Optional overrides: `APP_NAME` (default `tms-adapter`), `AWS_REGION` (default `us-east-2`).
