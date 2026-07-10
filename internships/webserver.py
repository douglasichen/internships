"""Static file server for the web UI, plus a few local-only endpoints so the
frontend can trigger a scrape or a --recompute run itself instead of needing
the terminal, and show whether one is currently live (started from here or
from a plain terminal `python3 -m internships`/`--recompute` -- both take
the same .run.lock, so a lock probe catches either). No auth -- this is a
personal tool meant to be run on localhost, not exposed beyond your own
machine.

Usage:
    python3 -m internships.webserver [PORT]   # defaults to 8765, blocks
    python3 -m internships.webserver --selftest
"""
import fcntl
import json
import sys
import threading
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from internships import __main__ as main_mod
from internships import recompute as recompute_mod
from internships.service import ROOT, run as run_sources

LOCK_PATH = ROOT / ".run.lock"
VALID_FIELDS = ("dedup", "is_2027", "priority", "descriptions")  # dedup first: no point
# backfilling a description for a row that's about to be dropped as a dupe

_state_lock = threading.Lock()
_state = {"running": False, "result": None, "error": None}

_scrape_lock = threading.Lock()
_scrape_state = {"running": False, "result": None, "error": None}


def _is_lock_held():
    """Non-blocking probe: is .run.lock currently held by ANY process --
    this server's own scrape/recompute thread, or a bare terminal
    `python3 -m internships`/`--recompute` run elsewhere? Purely a status
    check -- never holds the lock itself past the probe."""
    probe = open(LOCK_PATH, "w")
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(probe, fcntl.LOCK_UN)
        return False
    finally:
        probe.close()


def _run_scrape():
    # same lock as --recompute and a terminal scrape -- see _run_recompute's
    # comment on why (out/all.json shouldn't be rewritten by two processes
    # at once).
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        with _scrape_lock:
            _scrape_state["running"] = False
            _scrape_state["error"] = "a scrape or recompute is already running"
        return
    try:
        result = run_sources(main_mod.SOURCES)
        summary = {name: {"fetched": s.fetched, "swe": s.swe, "new": s.new, "error": s.error}
                   for name, s in result.per_source.items()}
        summary["new_listings"] = len(result.new_listings)
        if result.new_listings:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            scraped_at = datetime.now().isoformat(timespec="seconds")
            main_mod.write_csv(result.new_listings, scraped_at, main_mod.OUT_DIR / f"{ts}.csv")
            main_mod.append_all_json(result.new_listings, scraped_at)
        with _scrape_lock:
            _scrape_state["result"] = summary
            _scrape_state["error"] = None
    except Exception as e:  # noqa: BLE001 - report to the frontend, don't crash the server
        with _scrape_lock:
            _scrape_state["error"] = str(e)
    finally:
        with _scrape_lock:
            _scrape_state["running"] = False
        lock_file.close()


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
        if "priority" in fields:
            changed, total = recompute_mod.recompute_priority()
            summary["priority"] = f"{changed}/{total} changed"
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
        path = urlparse(self.path).path
        if path == "/api/recompute":
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
            return
        if path == "/api/scrape":
            with _scrape_lock:
                if _scrape_state["running"]:
                    self._json(409, {"error": "a scrape is already running"})
                    return
                _scrape_state["running"], _scrape_state["result"], _scrape_state["error"] = True, None, None
            threading.Thread(target=_run_scrape, daemon=True).start()
            self._json(202, {"started": True})
            return
        self.send_error(404)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/recompute/status":
            with _state_lock:
                self._json(200, dict(_state))
            return
        if path == "/api/scrape/status":
            with _scrape_lock:
                self._json(200, dict(_scrape_state))
            return
        if path == "/api/status":
            self._json(200, {"active": _is_lock_held()})
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

        # /api/status: reflects .run.lock state regardless of what's holding
        # it (a real lock held elsewhere in this same process, simulating a
        # bare terminal scrape/--recompute -- not this server's own state).
        status = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/status", timeout=5).read())
        assert status == {"active": False}, status
        outside_lock = open(LOCK_PATH, "w")
        fcntl.flock(outside_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            status = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/status", timeout=5).read())
            assert status == {"active": True}, status
        finally:
            fcntl.flock(outside_lock, fcntl.LOCK_UN)
            outside_lock.close()

        # /api/scrape: monkeypatch run_sources so this hits no network
        global run_sources
        orig_run_sources = run_sources
        class _FakeStats:
            fetched, swe, new, error = 1, 1, 1, ""
        class _FakeResult:
            new_listings = []
            per_source = {"fake": _FakeStats()}
        run_sources = lambda sources: _FakeResult()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/scrape", method="POST")
            r = urllib.request.urlopen(req, timeout=5)
            assert r.status == 202
            assert json.loads(r.read()) == {"started": True}

            for _ in range(200):
                s = json.loads(urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/scrape/status", timeout=5).read())
                if not s["running"]:
                    break
                time.sleep(0.01)
            assert s["result"]["new_listings"] == 0, s
            assert s["result"]["fake"] == {"fetched": 1, "swe": 1, "new": 1, "error": ""}, s
        finally:
            run_sources = orig_run_sources
    finally:
        httpd.shutdown()
    print("webserver selftest OK")


def main():
    if "--selftest" in sys.argv:
        selftest()
        return
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8765
    httpd = ThreadingHTTPServer(("", port), Handler)
    print(f"serving {ROOT} on :{port} (static files + POST /api/recompute + /api/scrape)")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
