"""API HTTP (stdlib) que esconde o protocolo TCP do TMS. Uso: python3 -m adapter.server
Toda rota exige `Authorization: Bearer <TMS_TOKEN>`."""
import hmac
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .service import BadInput, Service, Unavailable
from .tms_client import TmsClient, TmsReject


def load_env():
    env = dict(os.environ)
    f = Path(__file__).resolve().parent.parent / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip("\"'"))
    return env


def make_handler(svc, api_token):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):   # sem query string nem headers (token)
            sys.stderr.write("%s %s\n" % (self.command, self.path.split("?")[0]))

        def _send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self):
            auth = self.headers.get("Authorization", "")
            return auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].encode(), api_token.encode())

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                b = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                raise BadInput("JSON inválido")
            if not isinstance(b, dict):
                raise BadInput("corpo deve ser um objeto JSON")
            return b

        def _dispatch(self, method):
            if method == "GET" and self.path == "/healthz":   # liveness p/ o orquestrador: sem auth, não toca o TMS
                return self._send(200, {"status": "ok"})
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if method == "GET" and u.path == "/health":
                    return self._send(200, svc.health())
                if method == "GET" and u.path == "/loads/search":
                    return self._send(200, svc.search(q.get("origin_city"), q.get("origin_state"), q.get("dest_state"),
                                                      q.get("equipment"), q.get("pickup_date")))
                m = re.fullmatch(r"/loads/(LD\d+)", u.path)
                if method == "GET" and m:
                    return self._send(200, svc.get_load(m.group(1)))
                if method == "POST" and u.path == "/negotiation/evaluate":
                    b = self._body()
                    return self._send(200, svc.evaluate(b.get("load_id"), b.get("ask")))
                m = re.fullmatch(r"/loads/(LD\d+)/book", u.path)
                if method == "POST" and m:
                    b = self._body()
                    return self._send(200, svc.book(m.group(1), b.get("mc_num"), b.get("agreed_rate"), b.get("call_id")))
                self._send(404, {"error": "not_found"})
            except BadInput as e:
                self._send(400, {"error": "bad_request", "detail": str(e)})
            except LookupError:
                self._send(404, {"error": "load_unavailable"})
            except TmsReject as e:
                self._send(404 if e.code in ("NOT_FOUND", "UNKNOWN_LOAD") else 502, {"error": e.code.lower()})
            except Unavailable:
                self._send(503, {"error": "TMS_UNAVAILABLE"})

        def do_GET(self): self._dispatch("GET")
        def do_POST(self): self._dispatch("POST")
    return H


def main():
    env = load_env()
    token = env["TMS_TOKEN"]
    svc = Service(TmsClient(env["TMS_HOST"], env["TMS_PORT"], token))
    host, port = env.get("HOST", "127.0.0.1"), int(env.get("PORT", 8080))
    print(f"adapter em http://{host}:{port}", file=sys.stderr)
    ThreadingHTTPServer((host, port), make_handler(svc, token)).serve_forever()


if __name__ == "__main__":
    main()
