#!/usr/bin/env python3
"""Varrimento somente-leitura do TMS (nenhum LOAD_BOOK): estados, EQTYPE, MAX_RESULTS,
filtros de data/ZIP, relação MAX_BUY x RATE e taxa de falha em 200 LOAD_GET."""
import json, sys, collections, statistics
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from adapter.tms_client import TmsClient, TmsError, TmsReject

env = {}
for l in (ROOT / ".env").read_text().splitlines():
    if "=" in l and not l.startswith("#"):
        k, v = l.split("=", 1); env[k.strip()] = v.strip().strip("\"'")
c = TmsClient(env["TMS_HOST"], env["TMS_PORT"], env["TMS_TOKEN"], sleep=lambda s: None)  # sem backoff p/ medir
STATES = "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()

def safe(fn, *a, **k):
    try: return fn(*a, **k)
    except (TmsError, TmsReject) as e: return e

loads = {}
for st in STATES:
    r = safe(c.query, ORIG_STATE=st, MAX_RESULTS=500)
    if isinstance(r, Exception): print(st, "falhou:", r); continue
    for x in r: loads[x["LOAD_ID"]] = x
print("cargas distintas:", len(loads))
eq = collections.Counter(x["EQTYPE"] for x in loads.values())
print("EQTYPE:", dict(eq)); print("STATUS:", dict(collections.Counter(x["STATUS"] for x in loads.values())))

r = safe(c.query, ORIG_STATE="GA", MAX_RESULTS=1000); print("MAX_RESULTS=1000 em GA ->", len(r) if not isinstance(r, Exception) else r)
r = safe(c.query, ORIG_STATE="GA", MAX_RESULTS=1); print("MAX_RESULTS=1 ->", len(r) if not isinstance(r, Exception) else r)
ref = next(iter(loads.values())) if loads else None
date_filter = {}
if ref:
    for name in ("PICKUP_DT", "PICKUP_DATE", "DATE"):
        for val in (ref["PICKUP_DT"], ref["PICKUP_DT"][:8]):
            r = safe(c.query, ORIG_STATE=ref["ORIG_STATE"], **{name: val})
            date_filter[f"{name}={val}"] = len(r) if not isinstance(r, Exception) else str(r)
    print("filtro de data:", date_filter)
    for name in ("ORIG_ZIP", "DEST_ZIP"):
        r = safe(c.query, **{name: ref[name]}); print(name, "->", len(r) if not isinstance(r, Exception) else r)
    r = safe(c.query, ORIG_CITY=ref["ORIG_CITY"][:4]); print("ORIG_CITY prefixo ->", len(r) if not isinstance(r, Exception) else r)

# MAX_BUY x RATE e fuso/padding
gets, ratios = {}, []
for lid in list(loads)[:60]:
    g = safe(c.get, lid)
    if not isinstance(g, Exception):
        gets[lid] = g
        if "MAX_BUY" in g: ratios.append(g["MAX_BUY"] / g["RATE"])
if ratios:
    print(f"MAX_BUY/RATE: n={len(ratios)} min={min(ratios):.2f} med={statistics.median(ratios):.2f} max={max(ratios):.2f}; abaixo de 1: {sum(r<1 for r in ratios)}")
print("sem MAX_BUY:", sum("MAX_BUY" not in g for g in gets.values()))

# taxa de falha: 200 GET sem retry
target = next(iter(loads), None); stats = collections.Counter(); c.metrics.latencies_ms.clear()
if target:
    for _ in range(200):
        r = safe(c.call, "LOAD_GET", [("LOAD_ID", target)], 0)
        stats["ok" if not isinstance(r, Exception) else getattr(r, "kind", getattr(r, "code", "?"))] += 1
    lat = sorted(c.metrics.latencies_ms)
    print("200 LOAD_GET:", dict(stats), f"p50={lat[len(lat)//2]}ms p95={lat[int(len(lat)*.95)]}ms")

out = {"n_loads": len(loads), "eqtype": dict(eq), "date_filter": date_filter, "get_failure_stats": dict(stats),
       "loads": list(loads.values()), "gets_sample": list(gets.values())[:10]}
(ROOT / "discovery/results/tms_sweep.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
