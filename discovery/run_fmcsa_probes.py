#!/usr/bin/env python3
"""Sondagem inicial da FMCSA QCMobile API (3 testes por endpoint).

Lê FMCSA_API_KEY do .env, mascara a webKey e os telefones em tudo que grava
e salva o resultado bruto em discovery/results/fmcsa_results.json.
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = Path(__file__).resolve().parent / "results"
BASE = "https://mobile.fmcsa.dot.gov/qc/services"
TIMEOUT_S = 10


def load_env(path):
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env(ROOT / ".env")
KEY = ENV["FMCSA_API_KEY"]


def mask_url(url):
    return re.sub(r"webKey=[^&]+", "webKey=***", url)


def mask_phone(value):
    digits = re.sub(r"\D", "", str(value))
    return f"***-***-{digits[-4:]}" if len(digits) >= 4 else "***"


def scrub(obj):
    """Mascara telefones/fax recursivamente."""
    if isinstance(obj, dict):
        return {
            k: (mask_phone(v) if v and re.search(r"phone|fax", k, re.I) else scrub(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [scrub(x) for x in obj]
    return obj


def get(path, params=None, key=None):
    q = {"webKey": key or KEY, **(params or {})}
    url = f"{BASE}{path}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    t0 = time.monotonic()
    status, body, err = None, None, None
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except Exception as e:  # timeout, DNS, TLS
        raw, err = b"", f"{type(e).__name__}: {e}"
    latency = round((time.monotonic() - t0) * 1000)
    text = raw.decode("utf-8", errors="replace")
    try:
        body = json.loads(text) if text else None
    except json.JSONDecodeError:
        body = {"_non_json": text[:500]}
    return {"url": mask_url(url), "status": status, "latency_ms": latency, "error": err, "body": body}


def carriers_in(body):
    """Extrai a lista de objetos carrier, tolerando lista/objeto/nulo."""
    if not isinstance(body, dict):
        return []
    c = body.get("content")
    if isinstance(c, list):
        return [x.get("carrier", x) for x in c if isinstance(x, dict)]
    if isinstance(c, dict):
        return [c.get("carrier", c)]
    return []


def content_kind(body):
    if not isinstance(body, dict) or "content" not in body:
        return "no_content_key"
    c = body["content"]
    if c is None:
        return "null"
    if isinstance(c, list):
        return f"list[{len(c)}]"
    return type(c).__name__


results = []


def run(test_id, endpoint, desc, path, expect, params=None, key=None, check=None):
    r = get(path, params, key)
    carriers = carriers_in(r["body"])
    observed = {
        "status": r["status"],
        "content_kind": content_kind(r["body"]),
        "top_keys": sorted(r["body"].keys()) if isinstance(r["body"], dict) else None,
        "carrier_fields": sorted(carriers[0].keys()) if carriers and isinstance(carriers[0], dict) else None,
    }
    verdict = check(r, carriers) if check else ("ok" if r["status"] == 200 else "divergent")
    rec = {
        "id": test_id, "endpoint": endpoint, "description": desc, "expected": expect,
        "observed": observed, "verdict": verdict, **{k: r[k] for k in ("url", "latency_ms", "error")},
        "body": scrub(r["body"]),
    }
    results.append(rec)
    print(f"[{verdict:9}] {test_id:12} {r['status']} {r['latency_ms']:>5}ms {observed['content_kind']:14} {desc}")
    return r, carriers


def ok200(r, c):
    return "ok" if r["status"] == 200 and c else "divergent"


def notfound(r, c):
    # 404/400 ou 200 com content vazio = esperado; 500 = diverge da doc
    if r["status"] in (400, 404) or (r["status"] == 200 and not c):
        return "ok"
    return "divergent"


def first(c, *names):
    for n in names:
        if c and c[0].get(n) not in (None, ""):
            return c[0][n]
    return None


# ---- /carriers/name/{name} -------------------------------------------------
_, grey = run("FM-NAME-1", "/carriers/name/{name}", "greyhound (exemplo oficial)",
              "/carriers/name/greyhound", "200, lista de carriers", check=ok200)
_, freight = run("FM-NAME-2", "/carriers/name/{name}", "carrier de carga, paginação size=5",
                 "/carriers/name/" + urllib.parse.quote("swift transportation"),
                 "200, até 5 carriers", params={"start": 0, "size": 5}, check=ok200)
run("FM-NAME-3", "/carriers/name/{name}", "nome sem resultado",
    "/carriers/name/zzzqqxxnotacarrier", "200 vazio ou 404", check=notfound)

# DOTs fixos e conhecidos (confirmados na 1ª rodada exploratória):
#   54283   SWIFT TRANSPORTATION CO OF ARIZONA, MC 136818, autoridade ativa
#   1198688 "SWIFT TRANSPORTATION" (Denver), statusCode I, sem autoridade, mas allowedToOperate=Y
dot_a, dot_b = 54283, 1198688
DOT_BAD = "99999999999"
print(f"  -> DOT ativo: {dot_a} | DOT inativo: {dot_b}")

# ---- /carriers/{dot} -------------------------------------------------------
run("FM-DOT-1", "/carriers/{dot}", f"DOT ativo {dot_a}", f"/carriers/{dot_a}", "200, objeto carrier", check=ok200)
run("FM-DOT-2", "/carriers/{dot}", f"DOT inativo {dot_b}", f"/carriers/{dot_b}", "200, objeto carrier", check=ok200)
run("FM-DOT-3", "/carriers/{dot}", "DOT não numérico 'abc'", "/carriers/abc", "400/404 ou vazio", check=notfound)

# ---- sub-recursos do DOT ---------------------------------------------------
mc_a = None
for sub, label in [("authority", "AUTH"), ("oos", "OOS"), ("docket-numbers", "DKTS"), ("basics", "BASICS")]:
    r1, _ = run(f"FM-{label}-1", f"/carriers/{{dot}}/{sub}", f"DOT ativo {dot_a}",
                f"/carriers/{dot_a}/{sub}", "200")
    run(f"FM-{label}-2", f"/carriers/{{dot}}/{sub}", f"DOT inativo {dot_b}",
        f"/carriers/{dot_b}/{sub}", "200")
    run(f"FM-{label}-3", f"/carriers/{{dot}}/{sub}", f"DOT inexistente {DOT_BAD}",
        f"/carriers/{DOT_BAD}/{sub}", "404 ou vazio", check=notfound)
    if sub == "docket-numbers" and isinstance(r1["body"], dict):
        for d in r1["body"].get("content") or []:
            if isinstance(d, dict) and str(d.get("prefix", "")).upper() == "MC":
                mc_a = str(d.get("docketNumber"))
                break
print(f"  -> MC descoberto para o DOT {dot_a}: {mc_a}")

# ---- /carriers/docket-number/{mc} -----------------------------------------
if mc_a:
    def same_dot(r, c):
        return "ok" if r["status"] == 200 and c and str(c[0].get("dotNumber")) == str(dot_a) else "divergent"
    run("FM-DOCKET-1", "/carriers/docket-number/{mc}", f"MC ativo {mc_a}",
        f"/carriers/docket-number/{mc_a}", "200, carrier do mesmo DOT", check=same_dot)
    run("FM-DOCKET-2", "/carriers/docket-number/{mc}", f"MC com zeros à esquerda 00{mc_a}",
        f"/carriers/docket-number/00{mc_a}", "mesmo carrier (normaliza) ou vazio", check=same_dot)
else:
    print("  !! nenhum MC encontrado; DOCKET-1/2 pulados")
run("FM-DOCKET-3", "/carriers/docket-number/{mc}", "MC inexistente 99999999",
    "/carriers/docket-number/99999999", "404 ou content vazio", check=notfound)

# ---- teste extra: webKey inválida -----------------------------------------
run("FM-KEY-X", "auth", "webKey inválida", "/carriers/name/greyhound", "401",
    key="invalid-key-000", check=lambda r, c: "ok" if r["status"] == 401 else "divergent")

RESULTS.mkdir(exist_ok=True)
out = {
    "run_at": datetime.now(timezone.utc).isoformat(),
    "base_url": BASE,
    "discovered": {"dot_active": dot_a, "dot_inactive": dot_b, "mc_active": mc_a},
    "results": results,
}
(RESULTS / "fmcsa_results.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
print(f"\n{len(results)} testes gravados em {RESULTS / 'fmcsa_results.json'}")
