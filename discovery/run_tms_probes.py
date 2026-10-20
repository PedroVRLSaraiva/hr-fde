#!/usr/bin/env python3
"""Sondagem inicial do TMS legado via TCP (3 testes por comando).

Lê TMS_HOST, TMS_PORT e TMS_TOKEN do .env, mascara o token em tudo que grava
e salva o resultado bruto em discovery/results/tms_results.json.

LOAD_BOOK é testado SÓ com LOAD_ID inexistente: nenhuma carga é consumida.
"""
import json
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = Path(__file__).resolve().parent / "results"
CONNECT_TIMEOUT_S = 2
READ_TIMEOUT_S = 8
MAX_FRAME = 4096
MAX_ATTEMPTS = 3
FAKE_LOAD = "LD9999999999"


def load_env(path):
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env(ROOT / ".env")
HOST, PORT, TOKEN = ENV.get("TMS_HOST"), int(ENV.get("TMS_PORT") or 0), ENV.get("TMS_TOKEN")
if not (HOST and PORT and TOKEN):
    sys.exit("Faltam TMS_HOST, TMS_PORT ou TMS_TOKEN no .env")


def mask(s):
    return s.replace(TOKEN, "***") if TOKEN else s


def encode(cmd, fields, token):
    parts = [f"CMD:{cmd}", f"AUTH:{token}"] + [f"{k}:{v}" for k, v in fields]
    return "|".join(parts) + "\r\n"


def send(line):
    """Uma conexão por comando. Lê até END/ERR, EOF ou timeout."""
    t0 = time.monotonic()
    buf, lines, outcome, note = b"", [], None, None
    try:
        sock = socket.create_connection((HOST, PORT), timeout=CONNECT_TIMEOUT_S)
    except Exception as e:
        return {"outcome": "connect_error", "note": f"{type(e).__name__}: {e}", "lines": [],
                "latency_ms": round((time.monotonic() - t0) * 1000)}
    sock.settimeout(READ_TIMEOUT_S)
    deadline = t0 + READ_TIMEOUT_S
    try:
        sock.sendall(line.encode("ascii"))
        while outcome is None:
            sock.settimeout(max(0.05, deadline - time.monotonic()))
            chunk = sock.recv(8192)
            if not chunk:
                outcome = "partial" if lines or buf else "empty_eof"
                break
            buf += chunk
            while b"\r\n" in buf:
                raw, buf = buf.split(b"\r\n", 1)
                if len(raw) + 2 > MAX_FRAME:
                    outcome, note = "malformed", "linha acima de 4096 bytes"
                    break
                try:
                    text = raw.decode("ascii")
                except UnicodeDecodeError:
                    outcome, note = "malformed", "bytes não-ASCII"
                    break
                lines.append(text)
                if text == "END":
                    outcome = "ok"
                    break
                if text.startswith("ERR|"):
                    outcome = "error_response"
                    break
    except socket.timeout:
        outcome = "timeout" if not lines and not buf else "partial"
    except Exception as e:
        outcome, note = "io_error", f"{type(e).__name__}: {e}"
    latency = round((time.monotonic() - t0) * 1000)
    # Terminação atrasada: resposta completa, mas o servidor ainda não fechou.
    if outcome in ("ok", "error_response"):
        try:
            sock.settimeout(0.3)
            if sock.recv(1) != b"":
                note = (note or "") + " bytes extras após END"
        except socket.timeout:
            note = (note or "") + " servidor não fechou a conexão (delayed close; fechamos nós)"
        except Exception:
            pass
    sock.close()
    if buf:
        lines.append(f"<<sem CRLF>> {buf.decode('ascii', 'replace')}")
    return {"outcome": outcome, "note": (note or "").strip() or None, "lines": lines, "latency_ms": latency}


def parse_record(line):
    rec = {}
    for pair in line.split("|"):
        if ":" in pair:
            k, v = pair.split(":", 1)
            rec[k] = v.rstrip()
    return rec


def malformed_reason(lines):
    """Sanidade mínima de schema nos registros de carga."""
    for ln in lines:
        if ln in ("END",) or ln.startswith(("ERR|", "ECHO|")):
            continue
        pairs = ln.split("|")
        if any(":" not in p for p in pairs):
            return f"par sem ':' em {ln[:60]!r}"
        r = parse_record(ln)
        lid = r.get("LOAD_ID", "")
        if lid and not (len(lid) <= 12 and lid.startswith("LD") and lid[2:].isdigit()):
            return f"LOAD_ID inválido {lid!r}"
    return None


results = []
transient = {"timeout", "partial", "malformed", "empty_eof", "io_error", "connect_error"}


def run(test_id, cmd, desc, fields, expect, check, token=None, retry=True):
    line = encode(cmd, fields, token or TOKEN)
    attempts = []
    for _ in range(MAX_ATTEMPTS if retry else 1):
        r = send(line)
        if r["outcome"] == "ok":
            bad = malformed_reason(r["lines"])
            if bad:
                r["outcome"], r["note"] = "malformed", bad
        attempts.append(r)
        if r["outcome"] not in transient:
            break
        time.sleep(0.3)
    final = attempts[-1]
    verdict = check(final) if final["outcome"] not in transient else "failed"
    rec = {
        "id": test_id, "command": cmd, "description": desc, "expected": expect,
        "request": mask(line.rstrip("\r\n")), "verdict": verdict,
        "final": {**final, "lines": [mask(x) for x in final["lines"]]},
        "attempts": [{"outcome": a["outcome"], "note": a["note"], "latency_ms": a["latency_ms"]} for a in attempts],
    }
    results.append(rec)
    first = mask(final["lines"][0])[:90] if final["lines"] else "-"
    print(f"[{verdict:9}] {test_id:13} tent={len(attempts)} {final['outcome']:14} {final['latency_ms']:>5}ms  {first}")
    return final


def err_is(code):
    return lambda r: "ok" if r["lines"] and r["lines"][0].startswith(f"ERR|CODE:{code}") else "divergent"


def records(r):
    return [parse_record(x) for x in r["lines"] if x != "END" and not x.startswith("ERR|")]


# ---- DEBUG_ECHO -------------------------------------------------------------
run("TMS-ECHO-1", "DEBUG_ECHO", "MSG:HELLO", [("MSG", "HELLO")],
    "ECHO|AUTH:OK|FIELDS_PARSED:3|MSG:HELLO + END",
    lambda r: "ok" if r["lines"] and "AUTH:OK" in r["lines"][0] and "FIELDS_PARSED:3" in r["lines"][0] else "divergent")
run("TMS-ECHO-2", "DEBUG_ECHO", "6 pares com campo desconhecido",
    [("MSG", "HELLO"), ("A", "1"), ("B", "x:y"), ("ZZZ_UNKNOWN", "ok")], "FIELDS_PARSED:6",
    lambda r: "ok" if r["lines"] and "FIELDS_PARSED:6" in r["lines"][0] else "divergent")
run("TMS-ECHO-3", "DEBUG_ECHO", "token inválido", [("MSG", "HELLO")], "ERR|CODE:AUTH_FAILED",
    err_is("AUTH_FAILED"), token="invalid-token-000", retry=False)

# ---- LOAD_QUERY -------------------------------------------------------------
q1 = run("TMS-QUERY-1", "LOAD_QUERY", "ORIG_STATE:GA, MAX_RESULTS:50",
         [("ORIG_STATE", "GA"), ("MAX_RESULTS", "50")], "registros + END",
         lambda r: "ok" if r["outcome"] == "ok" and records(r) else "divergent")
loads = records(q1)
# Se GA vier vazio, tenta outros estados para ter cargas reais para os próximos testes
for st in ("TX", "IL", "CA", "FL"):
    if loads:
        break
    extra = run(f"TMS-QUERY-1-{st}", "LOAD_QUERY", f"fallback ORIG_STATE:{st}",
                [("ORIG_STATE", st), ("MAX_RESULTS", "50")], "registros + END",
                lambda r: "ok" if r["outcome"] == "ok" and records(r) else "divergent")
    loads = records(extra)

if loads:
    ref = loads[0]
    filt = [("ORIG_STATE", ref["ORIG_STATE"]), ("DEST_STATE", ref["DEST_STATE"]), ("EQTYPE", ref["EQTYPE"])]

    def q2_check(r):
        recs = records(r)
        good = recs and all(x.get("ORIG_STATE") == ref["ORIG_STATE"] and x.get("DEST_STATE") == ref["DEST_STATE"]
                            and x.get("EQTYPE") == ref["EQTYPE"] for x in recs)
        return "ok" if good else "divergent"
    run("TMS-QUERY-2", "LOAD_QUERY", "filtros combinados de uma carga real", filt,
        "subconjunto batendo com os 3 filtros", q2_check)
run("TMS-QUERY-3a", "LOAD_QUERY", "sem filtros", [], "ERR|CODE:MISSING_FIELD", err_is("MISSING_FIELD"))
run("TMS-QUERY-3b", "LOAD_QUERY", "EQTYPE inválido", [("ORIG_STATE", "GA"), ("EQTYPE", "XYZ")],
    "ERR|CODE:MISSING_FIELD|MSG:Invalid EQTYPE",
    lambda r: "ok" if r["lines"] and r["lines"][0].startswith("ERR|CODE:MISSING_FIELD|MSG:Invalid EQTYPE") else "divergent")

# ---- LOAD_GET ---------------------------------------------------------------
def get_check(r):
    recs = records(r)
    return "ok" if r["outcome"] == "ok" and recs and "DELIVERY_DT" in recs[0] else "divergent"


for i, tid in enumerate(("TMS-GET-1", "TMS-GET-2")):
    if len(loads) > i:
        run(tid, "LOAD_GET", f"carga real {loads[i]['LOAD_ID']}", [("LOAD_ID", loads[i]["LOAD_ID"])],
            "registro completo (MAX_BUY?) + END", get_check)
run("TMS-GET-3", "LOAD_GET", "id inexistente", [("LOAD_ID", FAKE_LOAD)], "ERR|CODE:NOT_FOUND",
    err_is("NOT_FOUND"))

# ---- LOAD_BOOK (só casos de erro, nunca com LOAD_ID real) -------------------
real_ids = {x.get("LOAD_ID") for x in loads}
assert FAKE_LOAD not in real_ids, "o id falso coincide com uma carga real; abortando"
run("TMS-BOOK-1", "LOAD_BOOK", "id inexistente",
    [("LOAD_ID", FAKE_LOAD), ("MC_NUM", "123456"), ("AGREED_RATE", "1000")], "ERR|CODE:UNKNOWN_LOAD",
    err_is("UNKNOWN_LOAD"))
run("TMS-BOOK-2", "LOAD_BOOK", "sem AGREED_RATE",
    [("LOAD_ID", FAKE_LOAD), ("MC_NUM", "123456")], "ERR|CODE:MISSING_FIELD", err_is("MISSING_FIELD"))
run("TMS-BOOK-3", "LOAD_BOOK", "AGREED_RATE:0 em id inexistente",
    [("LOAD_ID", FAKE_LOAD), ("MC_NUM", "123456"), ("AGREED_RATE", "0")],
    "INVALID_RATE ou UNKNOWN_LOAD (precedência)",
    lambda r: "ok" if r["lines"] and r["lines"][0].startswith(("ERR|CODE:INVALID_RATE", "ERR|CODE:UNKNOWN_LOAD")) else "divergent")

RESULTS.mkdir(exist_ok=True)
out = {
    "run_at": datetime.now(timezone.utc).isoformat(),
    "target": "TMS_HOST:TMS_PORT (do .env)",
    "loads_seen": [{k: v for k, v in x.items()} for x in loads],
    "results": results,
}
(RESULTS / "tms_results.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
print(f"\n{len(results)} testes gravados em {RESULTS / 'tms_results.json'}")
