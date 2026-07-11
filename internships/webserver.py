"""Static file server for the web UI, plus local-only JSON APIs.

Endpoints:
    POST /api/scrape, GET /api/scrape/status
    POST /api/recompute?fields=…, GET /api/recompute/status
    GET  /api/status                 # .run.lock held by anyone?
    GET  /api/descriptions/ids, POST /api/descriptions
    GET  /api/applied, POST /api/applied[?merge=1]
    POST /api/listings/clear-2027

Scrape/recompute share .run.lock with the CLI. No auth -- localhost personal
tool only; do not expose beyond your machine.

Usage:
    python3 -m internships.webserver [PORT]   # defaults to 8765, blocks
    python3 -m internships.webserver --selftest
"""
import fcntl
import gzip
import json
import sys
import threading
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from internships import __main__ as main_mod
from internships import applied_store, desc_store, recompute as recompute_mod
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


def _try_acquire_run_lock():
    """Non-blocking exclusive acquire of .run.lock.

    Returns an open file object holding the lock, or None if another
    scrape/recompute/writer already holds it. Caller must .close() the
    file to release (flock is tied to the fd).
    """
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None
    return lock_file


def _run_scrape():
    # same lock as --recompute and a terminal scrape -- see _run_recompute's
    # comment on why (out/all.json shouldn't be rewritten by two processes
    # at once).
    lock_file = _try_acquire_run_lock()
    if lock_file is None:
        with _scrape_lock:
            _scrape_state["running"] = False
            _scrape_state["error"] = "a scrape or recompute is already running"
        return
    try:
        # persist_seen=False: don't mark ids seen until out/all.json has the
        # rows -- same ordering as the CLI path in __main__.main().
        result = run_sources(main_mod.SOURCES, persist_seen=False)
        summary = {name: {"fetched": s.fetched, "swe": s.swe, "new": s.new, "error": s.error}
                   for name, s in result.per_source.items()}
        summary["new_listings"] = len(result.new_listings)
        if result.new_listings:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            scraped_at = datetime.now().isoformat(timespec="seconds")
            main_mod.write_csv(result.new_listings, scraped_at, main_mod.OUT_DIR / f"{ts}.csv")
            main_mod.append_all_json(result.new_listings, scraped_at)
        result.persist_seen()
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
    lock_file = _try_acquire_run_lock()
    if lock_file is None:
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

    def _read_json_body(self):
        """Parse a JSON request body. Returns (obj, None) or (None, error_str)."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None, "bad Content-Length"
        if n <= 0 or n > 2_000_000:  # ~2MB cap for pasted HTML/text
            return None, "body too large or empty"
        try:
            raw = self.rfile.read(n)
            data = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            return None, f"invalid JSON: {e}"
        if not isinstance(data, dict):
            return None, "JSON object required"
        return data, None

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
        if path == "/api/descriptions":
            # Manual submit of a missing job description (HTML or plain text).
            # desc_store.put is load-merge-save of the whole map -- must not
            # interleave with scrape/recompute append_all_json / backfill
            # which rewrite the same file under .run.lock.
            data, err = self._read_json_body()
            if err:
                self._json(400, {"error": err})
                return
            lid = data.get("id")
            text = data.get("text")
            if not isinstance(lid, str) or not lid.strip():
                self._json(400, {"error": "id required"})
                return
            if not isinstance(text, str) or not text.strip():
                self._json(400, {"error": "text required"})
                return
            lock_file = _try_acquire_run_lock()
            if lock_file is None:
                self._json(409, {"error": "a scrape or recompute is already running"})
                return
            try:
                desc_store.put(lid.strip(), text)
            except (OSError, ValueError, gzip.BadGzipFile, json.JSONDecodeError, EOFError) as e:
                self._json(500, {"error": str(e)})
                return
            finally:
                lock_file.close()
            self._json(200, {"ok": True, "id": lid.strip()})
            return
        if path == "/api/applied":
            # Full map replace or merge of applied marks {id: ISO timestamp}.
            # ?merge=1 unions with disk (later timestamp wins); default replaces.
            data, err = self._read_json_body()
            if err:
                self._json(400, {"error": err})
                return
            if not isinstance(data, dict):
                self._json(400, {"error": "object of id->ISO required"})
                return
            qs = parse_qs(urlparse(self.path).query)
            try:
                if qs.get("merge", ["0"])[0] in ("1", "true", "yes"):
                    marks = applied_store.merge(data)
                else:
                    applied_store.save(data)
                    marks = applied_store.load()
            except (OSError, ValueError) as e:
                self._json(500, {"error": str(e)})
                return
            self._json(200, {"ok": True, "count": len(marks), "marks": marks})
            return
        if path == "/api/listings/clear-2027":
            # Manual clear of is_2027 (UI confirmation happens client-side).
            # clear_is_2027 is a full read-modify-write of out/all.json --
            # same file scrape/recompute rewrite under .run.lock. Without
            # the lock a clear can clobber a concurrent append (lose new
            # rows) or have its override wiped by a concurrent recompute.
            data, err = self._read_json_body()
            if err:
                self._json(400, {"error": err})
                return
            lid = data.get("id")
            if not isinstance(lid, str) or not lid.strip():
                self._json(400, {"error": "id required"})
                return
            lock_file = _try_acquire_run_lock()
            if lock_file is None:
                self._json(409, {"error": "a scrape or recompute is already running"})
                return
            try:
                result = recompute_mod.clear_is_2027(lid.strip())
            except KeyError:
                self._json(404, {"error": "listing not found"})
                return
            except (OSError, ValueError) as e:
                self._json(500, {"error": str(e)})
                return
            finally:
                lock_file.close()
            self._json(200, result)
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
        if path == "/api/descriptions/ids":
            # Lightweight set of listing ids that already have a description
            # so the FE can badge rows missing one without loading full HTML.
            try:
                ids = list(desc_store.load().keys())
            except (OSError, ValueError) as e:
                self._json(500, {"error": str(e)})
                return
            self._json(200, {"ids": ids})
            return
        if path == "/api/applied":
            try:
                marks = applied_store.load()
            except (OSError, ValueError) as e:
                self._json(500, {"error": str(e)})
                return
            self._json(200, marks)
            return
        super().do_GET()

    def log_message(self, fmt, *args):
        pass  # ponytail: quiet by default; remove this override for request logs


def selftest():
    import tempfile
    import time
    import urllib.error
    import urllib.request
    from pathlib import Path

    # Isolate from a live webserver/scrape that may hold ROOT/.run.lock.
    global LOCK_PATH
    orig_lock = LOCK_PATH
    LOCK_PATH = Path(tempfile.mkdtemp()) / ".run.lock"

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

        # monkeypatch recompute + hold .run.lock only inside the worker path so
        # a live scrape elsewhere doesn't make the 409 race flake. Gate the
        # slow work on an Event so the second POST is guaranteed in-flight.
        orig = recompute_mod.dedupe
        hold = threading.Event()
        def slow_dedupe():
            hold.wait(timeout=2)
            return 3, 10
        recompute_mod.dedupe = slow_dedupe
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/recompute?fields=dedup",
                                          method="POST")
            r = urllib.request.urlopen(req, timeout=5)
            assert r.status == 202
            assert json.loads(r.read())["started"] == ["dedup"]

            for _ in range(100):
                with _state_lock:
                    if _state["running"]:
                        break
                time.sleep(0.01)
            else:
                raise AssertionError("recompute never set running=True")

            # a second recompute while one's running -> 409
            req2 = urllib.request.Request(f"http://127.0.0.1:{port}/api/recompute?fields=dedup",
                                           method="POST")
            try:
                urllib.request.urlopen(req2, timeout=5)
                raise AssertionError("expected HTTPError 409")
            except urllib.error.HTTPError as e:
                assert e.code == 409

            hold.set()
            for _ in range(200):
                status = json.loads(urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/recompute/status", timeout=5).read())
                if not status["running"]:
                    break
                time.sleep(0.01)
            assert status["result"] == {"dedup": "3/10 rows merged"}, status
        finally:
            hold.set()
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
            def persist_seen(self):
                pass
        run_sources = lambda sources, persist_seen=True: _FakeResult()
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

        # /api/descriptions/ids + POST /api/descriptions (in-memory, no disk)
        fake = {}
        orig_load, orig_put = desc_store.load, desc_store.put
        desc_store.load = lambda all_json_path=None: dict(fake)
        def _fake_put(lid, text, all_json_path=None):
            if lid and isinstance(text, str) and text:
                fake[lid] = text
        desc_store.put = _fake_put
        try:
            ids = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/descriptions/ids", timeout=5).read())
            assert ids == {"ids": []}, ids

            bad = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/descriptions",
                data=b'{"id":"x"}', method="POST",
                headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(bad, timeout=5)
                raise AssertionError("expected 400 for missing text")
            except urllib.error.HTTPError as e:
                assert e.code == 400

            body = json.dumps({"id": "job1", "text": "hello world"}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/descriptions",
                data=body, method="POST",
                headers={"Content-Type": "application/json"})
            r = urllib.request.urlopen(req, timeout=5)
            assert r.status == 200
            assert json.loads(r.read()) == {"ok": True, "id": "job1"}
            assert fake.get("job1") == "hello world"

            ids = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/descriptions/ids", timeout=5).read())
            assert ids == {"ids": ["job1"]}, ids
        finally:
            desc_store.load, desc_store.put = orig_load, orig_put

        # /api/applied: isolated temp store
        td_app = Path(tempfile.mkdtemp())
        orig_app_path = applied_store.path_for
        applied_store.path_for = lambda all_json_path=None: td_app / "applied.json"
        try:
            empty = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/applied", timeout=5).read())
            assert empty == {}, empty
            body = json.dumps({"aaa": "2026-01-01T00:00:00Z"}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/applied",
                data=body, method="POST",
                headers={"Content-Type": "application/json"})
            r = urllib.request.urlopen(req, timeout=5)
            assert r.status == 200
            got = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/applied", timeout=5).read())
            assert got == {"aaa": "2026-01-01T00:00:00Z"}, got
            # merge keeps later timestamp
            body2 = json.dumps({
                "aaa": "2025-01-01T00:00:00Z",
                "bbb": "2026-02-01T00:00:00Z",
            }).encode()
            req2 = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/applied?merge=1",
                data=body2, method="POST",
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req2, timeout=5)
            got2 = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/applied", timeout=5).read())
            assert got2["aaa"] == "2026-01-01T00:00:00Z", got2
            assert got2["bbb"] == "2026-02-01T00:00:00Z", got2
            # default POST replaces (does not union) — unapply path
            body3 = json.dumps({"bbb": "2026-02-01T00:00:00Z"}).encode()
            req3 = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/applied",
                data=body3, method="POST",
                headers={"Content-Type": "application/json"})
            r3 = urllib.request.urlopen(req3, timeout=5)
            assert r3.status == 200
            got3 = json.loads(r3.read())
            assert got3["marks"] == {"bbb": "2026-02-01T00:00:00Z"}, got3
            assert json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/applied", timeout=5).read()) == {
                "bbb": "2026-02-01T00:00:00Z"
            }
        finally:
            applied_store.path_for = orig_app_path

        # /api/listings/clear-2027
        td_clear = Path(tempfile.mkdtemp())
        clear_path = td_clear / "all.json"
        clear_path.write_text(json.dumps([
            {"id": "c1", "title": "SWE Intern Summer 2027", "location": "SF", "is_2027": True},
        ]))
        orig_clear = recompute_mod.clear_is_2027
        recompute_mod.clear_is_2027 = lambda lid, path=None: orig_clear(lid, clear_path)
        try:
            body = json.dumps({"id": "c1"}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/listings/clear-2027",
                data=body, method="POST",
                headers={"Content-Type": "application/json"})
            r = urllib.request.urlopen(req, timeout=5)
            assert r.status == 200
            assert json.loads(r.read())["is_2027"] is False
            row = json.loads(clear_path.read_text())[0]
            assert row["is_2027"] is False and row["is_2027_override"] is False
            miss = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/listings/clear-2027",
                data=b'{"id":"nope"}', method="POST",
                headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(miss, timeout=5)
                raise AssertionError("expected 404")
            except urllib.error.HTTPError as e:
                assert e.code == 404

            # while .run.lock is held, clear-2027 and descriptions must 409
            # (not clobber all.json / descriptions mid-scrape)
            held = open(LOCK_PATH, "w")
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                for path, payload in (
                    ("/api/listings/clear-2027", b'{"id":"c1"}'),
                    ("/api/descriptions", b'{"id":"x","text":"y"}'),
                ):
                    busy = urllib.request.Request(
                        f"http://127.0.0.1:{port}{path}",
                        data=payload, method="POST",
                        headers={"Content-Type": "application/json"})
                    try:
                        urllib.request.urlopen(busy, timeout=5)
                        raise AssertionError(f"expected 409 for {path} under lock")
                    except urllib.error.HTTPError as e:
                        assert e.code == 409, (path, e.code)
            finally:
                fcntl.flock(held, fcntl.LOCK_UN)
                held.close()
        finally:
            recompute_mod.clear_is_2027 = orig_clear
    finally:
        httpd.shutdown()
        LOCK_PATH = orig_lock
    print("webserver selftest OK")


def main():
    if "--selftest" in sys.argv:
        selftest()
        return
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8765
    httpd = ThreadingHTTPServer(("", port), Handler)
    print(f"serving {ROOT} on :{port} (static + scrape/recompute/descriptions/applied/clear-2027)")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
