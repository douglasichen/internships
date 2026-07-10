"""Static file server for the web UI, plus a couple of local-only POST
endpoints so the frontend can trigger a --recompute run itself instead of
needing the terminal. No auth -- this is a personal tool meant to be run on
localhost, not exposed beyond your own machine.

Usage:
    python3 -m internships.webserver [PORT]   # defaults to 8765, blocks
    python3 -m internships.webserver --selftest
"""
import fcntl
import json
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from internships import recompute as recompute_mod
from internships.service import ROOT

LOCK_PATH = ROOT / ".run.lock"
VALID_FIELDS = ("dedup", "is_2027", "important", "descriptions")  # dedup first: no point
# backfilling a description for a row that's about to be dropped as a dupe

_state_lock = threading.Lock()
_state = {"running": False, "result": None, "error": None}


def _run_recompute(fields):
    # same lock the CLI scrape/--recompute path uses -- out/all.json
    # shouldn't be rewritten by two processes (or a scrape + a recompute) at
    # once. held for the fields loop; ponytail: released via file close in
    # `finally`, not by name, since flock ties to the fd.
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        with _state_lock:
            _state["running"] = False
            _state["error"] = "a scrape or recompute is already running"
        return
    try:
        summary = {}
        if "dedup" in fields:
            removed, total = recompute_mod.dedupe()
            summary["dedup"] = f"{removed}/{total} rows merged"
        if "is_2027" in fields:
            changed, total = recompute_mod.recompute()
            summary["is_2027"] = f"{changed}/{total} changed"
        if "important" in fields:
            changed, total = recompute_mod.recompute_important()
            summary["important"] = f"{changed}/{total} changed"
        if "descriptions" in fields:
            changed, stale = recompute_mod.backfill_descriptions()
            summary["descriptions"] = f"{changed}/{stale} backfilled"
        with _state_lock:
            _state["result"] = summary
            _state["error"] = None
    except Exception as e:  # noqa: BLE001 - report to the frontend, don't crash the server
        with _state_lock:
            _state["error"] = str(e)
    finally:
        with _state_lock:
            _state["running"] = False
        lock_file.close()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if urlparse(self.path).path != "/api/recompute":
            self.send_error(404)
            return
        qs = parse_qs(urlparse(self.path).query)
        fields = [f for f in VALID_FIELDS if f in qs.get("fields", [""])[0].split(",")]
        with _state_lock:
            if _state["running"]:
                self._json(409, {"error": "a recompute is already running"})
                return
            if not fields:
                self._json(400, {"error": f"no valid fields (expected any of {VALID_FIELDS})"})
                return
            _state["running"], _state["result"], _state["error"] = True, None, None
        threading.Thread(target=_run_recompute, args=(fields,), daemon=True).start()
        self._json(202, {"started": fields})

    def do_GET(self):
        if urlparse(self.path).path == "/api/recompute/status":
            with _state_lock:
                self._json(200, dict(_state))
            return
        super().do_GET()

    def log_message(self, fmt, *args):
        pass  # ponytail: quiet by default; remove this override for request logs


def selftest():
    import time
    import urllib.error
    import urllib.request

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        # static serving still works (this is still SimpleHTTPRequestHandler)
        r = urllib.request.urlopen(f"http://127.0.0.1:{port}/CLAUDE.md", timeout=5)
        assert r.status == 200

        # invalid fields -> 400, nothing started
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/recompute?fields=bogus",
                                      method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("expected HTTPError 400")
        except urllib.error.HTTPError as e:
            assert e.code == 400

        # monkeypatch the actual recompute functions so this hits no network/disk
        orig = recompute_mod.dedupe
        def slow_dedupe():
            time.sleep(0.2)  # wide enough to reliably observe running=True below
            return 3, 10
        recompute_mod.dedupe = slow_dedupe
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/recompute?fields=dedup",
                                          method="POST")
            r = urllib.request.urlopen(req, timeout=5)
            assert r.status == 202
            assert json.loads(r.read())["started"] == ["dedup"]

            # a second recompute while one's running -> 409
            req2 = urllib.request.Request(f"http://127.0.0.1:{port}/api/recompute?fields=dedup",
                                           method="POST")
            for _ in range(50):  # give the background thread a moment to set running=True
                with _state_lock:
                    if _state["running"]:
                        break
                time.sleep(0.01)
            try:
                urllib.request.urlopen(req2, timeout=5)
                raise AssertionError("expected HTTPError 409")
            except urllib.error.HTTPError as e:
                assert e.code == 409

            for _ in range(200):  # poll status until the background thread finishes
                status = json.loads(urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/recompute/status", timeout=5).read())
                if not status["running"]:
                    break
                time.sleep(0.01)
            assert status["result"] == {"dedup": "3/10 rows merged"}, status
        finally:
            recompute_mod.dedupe = orig
    finally:
        httpd.shutdown()
    print("webserver selftest OK")


def main():
    if "--selftest" in sys.argv:
        selftest()
        return
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8765
    httpd = ThreadingHTTPServer(("", port), Handler)
    print(f"serving {ROOT} on :{port} (static files + POST /api/recompute)")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
