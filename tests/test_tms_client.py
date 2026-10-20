import socket, threading, unittest, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from adapter.tms_client import TmsClient, TmsError, TmsReject, encode, parse_record, validate_record

REC = ("LOAD_ID:LD01012     |ORIG_CITY:Atlanta                       |ORIG_STATE:GA|ORIG_ZIP:30303|"
       "DEST_CITY:Jacksonville                  |DEST_STATE:FL|DEST_ZIP:32202|PICKUP_DT:20261026130300|"
       "EQTYPE:FLATBED   |RATE:755     |MILES:285   |STATUS:OPEN    ")


def fake_server(responses):
    """Serve uma resposta por conexão; None = não responde; ('hang', bytes) = envia e não fecha."""
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(8)
    it = iter(responses)
    def run():
        for r in it:
            c, _ = srv.accept(); c.recv(4096)
            if isinstance(r, bytes): c.sendall(r)
            if r is None: threading.Event().wait(0.5)
            c.close()
    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1]


def client(port):
    return TmsClient("127.0.0.1", port, "SECRET", sleep=lambda s: None)


class T(unittest.TestCase):
    def test_encode_rejects_injection(self):
        for bad in ("Miami|MAX_RESULTS:999", "a\r\nb", "café"):
            with self.assertRaises(ValueError): encode("LOAD_QUERY", "t", [("ORIG_CITY", bad)])

    def test_parse_real_record(self):
        r = validate_record("LOAD_QUERY", parse_record(REC))
        self.assertEqual((r["LOAD_ID"], r["RATE"], r["MILES"]), ("LD01012", 755, 285))

    def test_partial_then_ok(self):
        port = fake_server([(REC + "\r\n").encode(), (REC + "\r\nEND\r\n").encode()])
        self.assertEqual(len(client(port).query(ORIG_STATE="GA")), 1)

    def test_err_not_retried(self):
        port = fake_server([b"ERR|CODE:NOT_FOUND|MSG:Load not found\r\n"])
        with self.assertRaises(TmsReject) as c: client(port).get("LD01012")
        self.assertEqual(c.exception.code, "NOT_FOUND")

    def test_malformed_width(self):
        bad = REC.replace("ORIG_STATE:GA", "ORIG_STATE:GAX")
        port = fake_server([(bad + "\r\nEND\r\n").encode()] * 4)
        with self.assertRaises(TmsError) as c: client(port).query(ORIG_STATE="GA")
        self.assertEqual(c.exception.kind, "malformed")

    def test_book_guard_rail(self):
        with self.assertRaises(ValueError): client(1).book("LD01012", "123", 900, 891)

    def test_book_no_blind_retry(self):
        port = fake_server([b"", ])
        with self.assertRaises(TmsError): client(port).book("LD01012", "123", 800, 891)

    def test_token_masked(self):
        self.assertEqual(client(1)._mask("AUTH:SECRET"), "AUTH:***")

if __name__ == "__main__": unittest.main()
