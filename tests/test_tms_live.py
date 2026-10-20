"""Somente leitura e erros contra o TMS real (nenhum LOAD_BOOK). Pulado sem .env."""
import json, pathlib, sys, threading, unittest, urllib.request, urllib.error
from http.server import ThreadingHTTPServer
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from adapter.server import load_env, make_handler
from adapter.service import Service
from adapter.tms_client import TmsClient

ENV = load_env()
HAVE = all(ENV.get(k) for k in ("TMS_HOST", "TMS_PORT", "TMS_TOKEN"))


@unittest.skipUnless(HAVE, "sem credenciais do TMS no .env")
class Live(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        svc = Service(TmsClient(ENV["TMS_HOST"], ENV["TMS_PORT"], ENV["TMS_TOKEN"]))
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(svc, ENV["TMS_TOKEN"]))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def req(self, path, body=None):
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"Authorization": "Bearer " + ENV["TMS_TOKEN"]})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_health(self):
        code, body = self.req("/health")
        self.assertEqual((code, json.loads(body)["tms"]), (200, "ok"))

    def test_search_by_name_and_no_leak(self):
        code, body = self.req("/loads/search?origin_state=Georgia")
        data = json.loads(body)
        self.assertEqual(code, 200); self.assertTrue(data["loads"])
        self.assertNotIn("max", body.lower())
        self.assertTrue(all(l["status"] == "OPEN" for l in data["loads"]))

    def test_evaluate_never_above_ceiling(self):
        lid = json.loads(self.req("/loads/search?origin_state=Georgia")[1])["loads"][0]["load_id"]
        d = json.loads(self.req("/negotiation/evaluate", {"load_id": lid, "ask": 10**6})[1])
        self.assertEqual(d["decision"], "counter")
        self.assertGreater(d["rate"], 0)
        self.assertEqual(json.loads(self.req("/negotiation/evaluate", {"load_id": lid, "ask": d["rate"]})[1])["decision"], "accept")

    def test_unknown_and_booked_load(self):
        self.assertEqual(self.req("/loads/LD9999999999")[0], 404)
        self.assertEqual(self.req("/loads/LD01031")[0], 404)   # já reservada (PENDING)

if __name__ == "__main__":
    unittest.main()
