import json
import pathlib
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from adapter.server import make_handler
from adapter.service import BadInput, Service, evaluate_offer, norm_equipment, norm_state, public_load
from adapter.tms_client import TmsError, TmsReject

G = {"LOAD_ID": "LD01031", "ORIG_CITY": "Atlanta", "ORIG_STATE": "GA", "ORIG_ZIP": "30303", "DEST_CITY": "Columbus",
     "DEST_STATE": "OH", "DEST_ZIP": "43004", "PICKUP_DT": "20991026130300", "DELIVERY_DT": "20991028150300",
     "EQTYPE": "POWER_ONLY", "RATE": 1000, "MAX_BUY": 1100, "MILES": 500, "STATUS": "OPEN", "WEIGHT": 100,
     "COMMODITY": "x", "PIECES": 1, "DIMS": "d", "NOTES": ""}


class Stub:
    """TmsClient falso, sem rede. `script` é a lista de efeitos de LOAD_BOOK."""
    def __init__(self, script=(), get_fn=None):
        self.script, self.calls = list(script), 0
        self.get_fn = get_fn or (lambda lid: dict(G))
        self.metrics = type("M", (), {"counts": {}})()

    def get(self, lid): return self.get_fn(lid)
    def query(self, **f): return [dict(G)]
    def echo(self): return []

    def call(self, cmd, fields, retries=None):
        self.calls += 1
        e = self.script.pop(0)
        if isinstance(e, Exception):
            raise e
        return [e]


OK = {"BOOKING_REF": "R1", "STATUS": "PENDING"}


class ServiceTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(norm_state("Georgia"), "GA")
        self.assertEqual(norm_state("tx"), "TX")
        self.assertEqual(norm_equipment("Dry Van"), "DRY_VAN")
        self.assertEqual(norm_equipment("step deck"), "STEP_DECK")
        with self.assertRaises(BadInput): norm_state("Atlantis")
        with self.assertRaises(BadInput): norm_equipment("canoe")

    def test_max_rate_exposed_in_search(self):
        self.assertEqual(public_load(G)["max_rate"], 1100)
        out = Service(Stub()).search(origin_state="Georgia", equipment="power only")
        self.assertEqual(out["loads"][0]["max_rate"], 1100)
        self.assertNotIn("MAX_BUY", json.dumps(out))
        self.assertEqual(out["loads"][0]["loadboard_rate"], 1000)
        self.assertTrue(out["loads"][0]["pickup_datetime"].endswith("Z"))

    def test_max_rate_in_load_detail(self):
        self.assertEqual(Service(Stub()).get_load("LD1")["max_rate"], 1100)

    def test_evaluate(self):
        self.assertEqual(evaluate_offer(1100, 1100), {"decision": "accept", "rate": 1100})
        self.assertEqual(evaluate_offer(1100, 900), {"decision": "accept", "rate": 900})
        self.assertEqual(evaluate_offer(1100, 1101), {"decision": "counter", "rate": 1100})
        for bad in (0, -5, "1000", True):
            with self.assertRaises(BadInput): evaluate_offer(1100, bad)

    def test_book_success_and_ceiling_guard(self):
        self.assertEqual(Service(Stub([OK])).book("LD01031", "MC123", 1100)["result"], "booked")
        s = Service(Stub())
        self.assertEqual(s.book("LD01031", "123", 1101), {"result": "error", "reason": "rate_above_ceiling"})
        self.assertEqual(s.tms.calls, 0)

    def test_book_not_open(self):
        s = Service(Stub(get_fn=lambda l: {**G, "STATUS": "PENDING"}))
        self.assertEqual(s.book("LD01031", "123", 1000), {"result": "unavailable"})

    def test_already_booked_first_is_unavailable(self):
        self.assertEqual(Service(Stub([TmsReject("ALREADY_BOOKED")])).book("LD01031", "1", 1000)["result"], "unavailable")

    def test_unknown_then_already_booked_reconciles(self):
        n = []
        def get(lid):
            n.append(1)
            return {**G, "STATUS": "OPEN" if len(n) == 1 else "PENDING"}
        s = Service(Stub([TmsError("timeout"), TmsReject("ALREADY_BOOKED")], get_fn=get))
        self.assertEqual(s.book("LD01031", "1", 1000)["result"], "booked_unconfirmed")

    def test_two_failures_need_human(self):
        s = Service(Stub([TmsError("timeout"), TmsError("partial")]))
        self.assertEqual(s.book("LD01031", "1", 1000), {"result": "error", "reason": "needs_human"})

    def test_timeout_then_success(self):
        self.assertEqual(Service(Stub([TmsError("timeout"), OK])).book("LD01031", "1", 1000)["result"], "booked")

    def test_concurrent_book_serialized(self):
        n, gate = [], threading.Lock()
        def get(lid):
            with gate: n.append(1)
            return {**G, "STATUS": "OPEN" if len(n) == 1 else "PENDING"}
        st = Stub([OK, TmsReject("ALREADY_BOOKED")])
        s = Service(st)
        res = []
        # a 2ª chamada vê a carga já PENDING depois do lock e responde unavailable sem tocar o TMS
        def get2(lid):
            return {**G, "STATUS": "PENDING" if st.calls else "OPEN"}
        st.get_fn = get2
        ts = [threading.Thread(target=lambda: res.append(s.book("LD01031", "1", 1000)["result"])) for _ in range(2)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(sorted(res), ["booked", "unavailable"])
        self.assertEqual(st.calls, 1)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Service(Stub([OK])), "tok"))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def req(self, path, body=None, token="tok"):
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"Authorization": f"Bearer {token}"} if token else {})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_healthz_is_open_and_minimal(self):
        self.assertEqual(self.req("/healthz", token=None), (200, '{"status": "ok"}'))

    def test_auth(self):
        self.assertEqual(self.req("/health", token=None)[0], 401)
        self.assertEqual(self.req("/health", token="wrong")[0], 401)
        self.assertEqual(self.req("/health")[0], 200)

    def test_no_ceiling_leak_and_flow(self):
        code, body = self.req("/loads/LD01031")
        self.assertEqual(code, 200); self.assertEqual(json.loads(body)["max_rate"], 1100); self.assertNotIn("MAX_BUY", body)
        self.assertEqual(json.loads(self.req("/loads/search?origin_state=GA")[1])["loads"][0]["max_rate"], 1100)
        code, body = self.req("/negotiation/evaluate", {"load_id": "LD01031", "ask": 1500})
        self.assertEqual(json.loads(body), {"decision": "counter", "rate": 1100})
        self.assertEqual(self.req("/negotiation/evaluate", {"load_id": "LD01031", "ask": "x"})[0], 400)
        self.assertEqual(self.req("/loads/search?origin_state=Atlantis")[0], 400)
        code, body = self.req("/loads/LD01031/book", {"mc_num": "123", "agreed_rate": 1050})
        self.assertEqual((code, json.loads(body)["result"]), (200, "booked"))


if __name__ == "__main__":
    unittest.main()
