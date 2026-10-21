"""Regras de negócio do adapter: normalização, busca em cascata, cache, negociação,
lock por carga e máquina de estados do booking. `max_rate` (MAX_BUY) sai em search e get_load."""
import re
import threading
import time
from datetime import datetime, timezone

from .tms_client import TmsClient, TmsError, TmsReject

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "district of columbia": "DC", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}
STATE_CODES = set(US_STATES.values())
EQUIPMENT = {
    "DRY_VAN": ("dry van", "dryvan", "van", "dry_van", "caixa seca"),
    "REEFER": ("reefer", "refrigerated", "refrigerado", "refrigerada", "cold"),
    "FLATBED": ("flatbed", "flat bed", "flat", "carreta aberta"),
    "STEP_DECK": ("step deck", "stepdeck", "step_deck", "drop deck"),
    "POWER_ONLY": ("power only", "power_only", "poweronly"),
}
CACHE_TTL_S = 45
LOCK_TIMEOUT_S = 120


class BadInput(ValueError):
    pass


class Unavailable(Exception):
    """TMS indisponível (circuit breaker aberto ou falhas esgotadas)."""


def norm_state(v):
    if not v:
        return None
    t = re.sub(r"\s+", " ", str(v).strip().lower().rstrip("."))
    if t.upper() in STATE_CODES:
        return t.upper()
    if t in US_STATES:
        return US_STATES[t]
    raise BadInput(f"estado desconhecido: {v!r}")


def norm_equipment(v):
    if not v:
        return None
    t = re.sub(r"\s+", " ", str(v).strip().lower())
    for code, names in EQUIPMENT.items():
        if t == code.lower() or t in names:
            return code
    raise BadInput(f"equipamento desconhecido: {v!r}")


def clean_text(v):
    return re.sub(r"[|\r\n]+", " ", str(v)).strip() if v else None


def iso_utc(dt):
    return datetime.strptime(dt, "%Y%m%d%H%M%S").isoformat() + "Z" if dt else None


def public_load(g):
    """Allowlist de campos para o agente. `max_rate` (MAX_BUY) é exposto de propósito, para o fluxo gravá-lo."""
    out = {
        "load_id": g["LOAD_ID"],
        "origin": {"city": g["ORIG_CITY"], "state": g["ORIG_STATE"], "zip": g.get("ORIG_ZIP")},
        "destination": {"city": g["DEST_CITY"], "state": g["DEST_STATE"], "zip": g.get("DEST_ZIP")},
        "pickup_datetime": iso_utc(g["PICKUP_DT"]),
        "delivery_datetime": iso_utc(g.get("DELIVERY_DT")),
        "equipment_type": g["EQTYPE"],
        "loadboard_rate": g["RATE"],
        "weight": g.get("WEIGHT"),
        "commodity_type": g.get("COMMODITY"),
        "num_of_pieces": g.get("PIECES"),
        "miles": g["MILES"],
        "dimensions": g.get("DIMS"),
        "notes": g.get("NOTES") or None,
        "status": g["STATUS"],
    }
    if "MAX_BUY" in g:
        out["max_rate"] = g["MAX_BUY"]
    return out


def is_bookable(rec, now=None):
    now = now or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return rec["STATUS"] == "OPEN" and rec["PICKUP_DT"] > now


class Breaker:
    def __init__(self, threshold=6, open_s=20):
        self.threshold, self.open_s, self.fails, self.opened_at = threshold, open_s, 0, 0.0

    @property
    def is_open(self):
        return self.fails >= self.threshold and time.monotonic() - self.opened_at < self.open_s

    def ok(self):
        self.fails = 0

    def fail(self):
        self.fails += 1
        if self.fails >= self.threshold:
            self.opened_at = time.monotonic()


def evaluate_offer(max_buy, ask):
    """MAX_BUY é a referência: qualquer ask <= MAX_BUY é válido; acima, contra no próprio teto."""
    if not isinstance(ask, int) or isinstance(ask, bool) or ask <= 0:
        raise BadInput("ask deve ser inteiro positivo")
    if ask <= max_buy:
        return {"decision": "accept", "rate": ask}
    return {"decision": "counter", "rate": max_buy}


class Service:
    def __init__(self, tms: TmsClient):
        self.tms = tms
        self.breaker = Breaker()
        self._cache = {}
        self._cache_lock = threading.Lock()
        self._locks = {}
        self._locks_guard = threading.Lock()
        self.bookings = {}   # em memória; trocar pelo Twin

    # --- infraestrutura ------------------------------------------------------
    def _guarded(self, fn, *a, **k):
        if self.breaker.is_open:
            raise Unavailable("circuit breaker aberto")
        try:
            r = fn(*a, **k)
            self.breaker.ok()
            return r
        except TmsError:
            self.breaker.fail()
            raise Unavailable("TMS indisponível")
        except TmsReject as e:
            if e.code in ("SERVER_ERROR", "AUTH_FAILED"):
                self.breaker.fail()
            raise

    def _cached(self, key, fn):
        now = time.monotonic()
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        val = fn()
        with self._cache_lock:
            self._cache[key] = (now + CACHE_TTL_S, val)
        return val

    def invalidate(self, load_id):
        with self._cache_lock:
            self._cache = {k: v for k, v in self._cache.items() if k != ("get", load_id) and k[0] != "query"}

    def _raw_get(self, load_id, fresh=False):
        if not re.fullmatch(r"LD\d{1,10}", load_id or ""):
            raise BadInput("load_id inválido")
        if fresh:
            return self._guarded(self.tms.get, load_id)
        return self._cached(("get", load_id), lambda: self._guarded(self.tms.get, load_id))

    # --- operações -----------------------------------------------------------
    def search(self, origin_city=None, origin_state=None, dest_state=None, equipment=None, pickup_date=None, limit=3):
        o, d, e = norm_state(origin_state), norm_state(dest_state), norm_equipment(equipment)
        city = clean_text(origin_city)
        date = re.sub(r"\D", "", pickup_date or "") or None
        if date and len(date) != 8:
            raise BadInput("pickup_date deve ser YYYYMMDD")
        base = {"ORIG_CITY": city, "ORIG_STATE": o, "DEST_STATE": d, "EQTYPE": e, "PICKUP_DATE": date}
        full = {k: v for k, v in base.items() if v}
        # cascata: tudo -> sem data -> sem destino -> sem cidade -> só origem
        attempts = [base,
                    {**base, "PICKUP_DATE": None},
                    {**base, "PICKUP_DATE": None, "DEST_STATE": None},
                    {**base, "PICKUP_DATE": None, "DEST_STATE": None, "ORIG_CITY": None},
                    {"ORIG_STATE": o}]
        seen = set()
        for f in attempts:
            f = {k: v for k, v in f.items() if v}
            key = tuple(sorted(f.items()))
            if not f or key in seen:
                continue
            seen.add(key)
            rows = self._cached(("query", key), lambda f=f: self._guarded(self.tms.query, MAX_RESULTS=20, **f))
            rows = sorted((r for r in rows if is_bookable(r)), key=lambda r: r["PICKUP_DT"])
            if rows:
                out = []
                for r in rows[:limit]:
                    try:
                        out.append(public_load(self._raw_get(r["LOAD_ID"])))
                    except (Unavailable, TmsReject):
                        out.append(public_load({**r, "DELIVERY_DT": None}))
                return {"loads": out, "relaxed": f != full}
        return {"loads": [], "relaxed": False}

    def get_load(self, load_id):
        g = self._raw_get(load_id)
        if not is_bookable(g):
            raise LookupError("carga indisponível")
        return public_load(g)

    def evaluate(self, load_id, ask):
        g = self._raw_get(load_id)
        if "MAX_BUY" not in g:
            raise Unavailable("sem MAX_BUY: teto indefinido")
        return evaluate_offer(g["MAX_BUY"], ask)

    def _lock_for(self, load_id):
        with self._locks_guard:
            return self._locks.setdefault(load_id, threading.Lock())

    def book(self, load_id, mc_num, agreed_rate, call_id=None):
        mc = re.sub(r"\D", "", str(mc_num or "").upper().replace("MC", ""))
        if not mc:
            raise BadInput("mc_num inválido")
        if not isinstance(agreed_rate, int) or isinstance(agreed_rate, bool) or agreed_rate <= 0:
            raise BadInput("agreed_rate deve ser inteiro positivo")
        lock = self._lock_for(load_id)
        if not lock.acquire(timeout=LOCK_TIMEOUT_S):
            return {"result": "error", "reason": "lock_timeout"}
        try:
            g = self._raw_get(load_id, fresh=True)
            if not is_bookable(g):
                return {"result": "unavailable"}
            if "MAX_BUY" not in g or agreed_rate > g["MAX_BUY"]:
                return {"result": "error", "reason": "rate_above_ceiling"}   # guard-rail final
            st = self.bookings[load_id] = {"state": "PENDING", "call_id": call_id, "mc": mc,
                                           "rate": agreed_rate, "at": time.time()}
            unknown = False
            for _ in range(2):
                try:
                    r = self.tms.call("LOAD_BOOK", [("LOAD_ID", load_id), ("MC_NUM", mc), ("AGREED_RATE", agreed_rate)], 0)[0]
                    st.update(state="BOOKED", booking_ref=r["BOOKING_REF"], tms_status=r["STATUS"])
                    return {"result": "booked", "booking_ref": r["BOOKING_REF"]}
                except TmsReject as e:
                    if e.code != "ALREADY_BOOKED":
                        st["state"] = "REJECTED"
                        return {"result": "error", "reason": e.code.lower()}
                    if not unknown:
                        st["state"] = "LOST"
                        return {"result": "unavailable"}
                    # após tentativa incerta, o TMS diz que foi ESTE token
                    try:
                        if self._raw_get(load_id, fresh=True)["STATUS"] in ("PENDING", "BOOKED"):
                            st["state"] = "BOOKED_UNCONFIRMED"
                            return {"result": "booked_unconfirmed"}
                    except (TmsError, TmsReject, Unavailable):
                        pass
                    st["state"] = "NEEDS_HUMAN"
                    return {"result": "error", "reason": "needs_human"}
                except TmsError:
                    unknown = True
                    st["state"] = "UNKNOWN"
            st["state"] = "NEEDS_HUMAN"
            return {"result": "error", "reason": "needs_human"}
        finally:
            self.invalidate(load_id)
            lock.release()

    def health(self):
        try:
            self.tms.echo()
            tms = "ok"
        except (TmsError, TmsReject) as e:
            tms = f"error:{getattr(e, 'kind', getattr(e, 'code', '?'))}"
        return {"adapter": "ok", "tms": tms, "breaker_open": self.breaker.is_open, "metrics": self.tms.metrics.counts}
