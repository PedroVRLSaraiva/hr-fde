"""Cliente do TMS legado (LTMS): TCP cru, uma requisição por conexão.

Valida cada resposta pelo protocolo (END obrigatório, schema por campo) e
trata como falha transitória timeout, resposta parcial e malformada.
O token nunca aparece em logs nem em exceções (mascarado).
"""
import re
import socket
import threading
import time
import random
from dataclasses import dataclass, field

MAX_FRAME = 4096
CONNECT_TIMEOUT_S = 2.0
READ_TIMEOUT_S = {"LOAD_QUERY": 5.0, "LOAD_GET": 5.0, "LOAD_BOOK": 8.0, "DEBUG_ECHO": 5.0}
BACKOFF_S = (0.2, 0.5, 1.0)

# Larguras máximas (seção 4 do guia). LOAD_ID real: LD + 5 dígitos; aceitar qualquer quantidade.
WIDTHS = {
    "LOAD_ID": 12, "ORIG_CITY": 30, "ORIG_STATE": 2, "ORIG_ZIP": 5, "DEST_CITY": 30, "DEST_STATE": 2,
    "DEST_ZIP": 5, "PICKUP_DT": 14, "DELIVERY_DT": 14, "EQTYPE": 10, "RATE": 7, "MAX_BUY": 7,
    "WEIGHT": 7, "COMMODITY": 32, "PIECES": 6, "MILES": 6, "DIMS": 35, "NOTES": 114, "STATUS": 8,
    "BOOKING_REF": 16, "TIMESTAMP": 14,
}
INT_FIELDS = {"RATE", "MAX_BUY", "WEIGHT", "PIECES", "MILES"}
REQUIRED = {
    "LOAD_QUERY": {"LOAD_ID", "ORIG_CITY", "ORIG_STATE", "DEST_CITY", "DEST_STATE", "PICKUP_DT", "EQTYPE", "RATE", "MILES", "STATUS"},
    "LOAD_GET": {"LOAD_ID", "ORIG_STATE", "DEST_STATE", "PICKUP_DT", "DELIVERY_DT", "EQTYPE", "RATE", "STATUS"},
    "LOAD_BOOK": {"LOAD_ID", "BOOKING_REF", "STATUS", "TIMESTAMP"},
}
RETRYABLE_ERR = {"SERVER_ERROR"}
LOAD_ID_RE = re.compile(r"^LD\d+$")


class TmsError(Exception):
    """Falha de protocolo/transporte. `kind` ∈ timeout|partial|malformed|connect|io."""
    def __init__(self, kind, msg=""):
        super().__init__(f"{kind}: {msg}")
        self.kind = kind


class TmsReject(Exception):
    """Resposta ERR do TMS (definitiva, salvo SERVER_ERROR)."""
    def __init__(self, code, msg=""):
        super().__init__(f"{code}: {msg}")
        self.code, self.msg = code, msg


def encode(cmd, token, fields=()):
    """Monta a linha; rejeita valores com `|`, CR ou LF (injeção de campos) e não-ASCII."""
    parts = [f"CMD:{cmd}", f"AUTH:{token}"]
    for k, v in fields:
        v = str(v)
        if any(c in v for c in "|\r\n") or not v.isascii():
            raise ValueError(f"valor inválido para {k}")
        parts.append(f"{k}:{v}")
    line = "|".join(parts) + "\r\n"
    if len(line) > MAX_FRAME:
        raise ValueError("requisição acima de 4096 bytes")
    return line


def parse_record(line):
    rec = {}
    for pair in line.split("|"):
        if ":" not in pair:
            raise TmsError("malformed", "par sem ':'")
        k, v = pair.split(":", 1)
        rec[k] = v.rstrip()
    return rec


def validate_record(cmd, rec):
    for k, v in rec.items():
        w = WIDTHS.get(k)
        if w and len(v) > w:
            raise TmsError("malformed", f"{k} acima da largura {w}")
        if k in INT_FIELDS and not v.isdigit():
            raise TmsError("malformed", f"{k} não numérico")
        if k == "LOAD_ID" and not LOAD_ID_RE.match(v):
            raise TmsError("malformed", "LOAD_ID inválido")
    missing = REQUIRED.get(cmd, set()) - rec.keys()
    if missing:
        raise TmsError("malformed", f"faltam campos {sorted(missing)}")
    for k in INT_FIELDS & rec.keys():
        rec[k] = int(rec[k])
    return rec


def read_response(sock, deadline):
    """Lê linhas até END ou ERR (fecha do nosso lado). Sem END → partial."""
    buf, lines = b"", []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TmsError("partial" if (lines or buf) else "timeout")
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(8192)
        except socket.timeout:
            raise TmsError("partial" if (lines or buf) else "timeout")
        if not chunk:
            raise TmsError("partial" if (lines or buf) else "timeout", "EOF antes do END")
        buf += chunk
        while b"\r\n" in buf:
            raw, buf = buf.split(b"\r\n", 1)
            if len(raw) + 2 > MAX_FRAME:
                raise TmsError("malformed", "linha acima de 4096 bytes")
            try:
                text = raw.decode("ascii")
            except UnicodeDecodeError:
                raise TmsError("malformed", "não-ASCII")
            if text == "END":
                return lines
            if text.startswith("ERR|"):
                err = parse_record(text[4:])
                raise TmsReject(err.get("CODE", "UNKNOWN"), err.get("MSG", ""))
            lines.append(text)
        if len(buf) > MAX_FRAME:
            raise TmsError("malformed", "linha sem CRLF acima de 4096 bytes")


@dataclass
class Metrics:
    counts: dict = field(default_factory=dict)
    latencies_ms: list = field(default_factory=list)

    def bump(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1


class TmsClient:
    def __init__(self, host, port, token, max_concurrency=10, sleep=time.sleep):
        self.host, self.port, self._token = host, int(port), token
        self._sem = threading.BoundedSemaphore(max_concurrency)
        self._sleep = sleep
        self.metrics = Metrics()

    def _mask(self, s):
        return s.replace(self._token, "***")

    def _once(self, cmd, fields):
        line = encode(cmd, self._token, fields)
        t0 = time.monotonic()
        deadline = t0 + READ_TIMEOUT_S.get(cmd, 5.0)
        try:
            sock = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT_S)
        except OSError as e:
            raise TmsError("connect", type(e).__name__)
        try:
            sock.sendall(line.encode("ascii"))
            lines = read_response(sock, deadline)
        finally:
            sock.close()  # uma requisição por conexão; fechamos ao ler END/ERR
            self.metrics.latencies_ms.append(round((time.monotonic() - t0) * 1000))
        recs = [validate_record(cmd, parse_record(x)) for x in lines] if cmd != "DEBUG_ECHO" else [parse_record(x.replace("ECHO|", "ECHO:1|", 1)) for x in lines]
        return recs

    def call(self, cmd, fields=(), retries=None):
        """QUERY/GET: até 3 tentativas com backoff+jitter. BOOK: nunca retry cego (retries=0)."""
        if retries is None:
            retries = 0 if cmd == "LOAD_BOOK" else len(BACKOFF_S)
        with self._sem:
            for attempt in range(retries + 1):
                try:
                    recs = self._once(cmd, fields)
                    self.metrics.bump(f"{cmd}.ok")
                    return recs
                except TmsReject as e:
                    self.metrics.bump(f"{cmd}.err.{e.code}")
                    if e.code not in RETRYABLE_ERR or attempt == retries:
                        raise
                except TmsError as e:
                    self.metrics.bump(f"{cmd}.{e.kind}")
                    if attempt == retries:
                        raise
                self._sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)] * (0.5 + random.random()))

    # --- API de alto nível ---------------------------------------------------
    def echo(self, n_extra=0):
        extra = [(f"F{i}", "x") for i in range(n_extra)]
        return self.call("DEBUG_ECHO", [("MSG", "HELLO")] + extra)

    def query(self, **filters):
        return self.call("LOAD_QUERY", [(k.upper(), v) for k, v in filters.items()])

    def get(self, load_id):
        recs = self.call("LOAD_GET", [("LOAD_ID", load_id)])
        if len(recs) != 1:
            raise TmsError("malformed", "LOAD_GET deve devolver 1 registro")
        return recs[0]

    def book(self, load_id, mc_num, agreed_rate, max_buy):
        """Guard-rail: o TMS não aplica o teto, então validamos aqui."""
        if not str(mc_num).isdigit():
            raise ValueError("MC_NUM só dígitos")
        if not isinstance(agreed_rate, int) or agreed_rate <= 0:
            raise ValueError("AGREED_RATE inteiro positivo")
        if agreed_rate > max_buy:
            raise ValueError("AGREED_RATE acima do MAX_BUY")
        recs = self.call("LOAD_BOOK", [("LOAD_ID", load_id), ("MC_NUM", mc_num), ("AGREED_RATE", agreed_rate)])
        return recs[0]
