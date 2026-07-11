"""Manual "applied" marks: id -> ISO timestamp, stored next to all.json.

Browser localStorage alone is fragile (localhost vs 127.0.0.1 are different
origins). This file is the durable source of truth on disk; the FE still
mirrors into localStorage for offline/static opens.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from internships.service import ROOT

APPLIED_PATH = ROOT / "out" / "applied.json"

# ThreadingHTTPServer can handle concurrent POST /api/applied. save/merge are
# full-file rewrites via a shared *.tmp name; without a lock two writers
# clobber each other's tmp (FileNotFoundError) or drop keys (lost update).
_lock = threading.Lock()


def path_for(all_json_path=None) -> Path:
    if all_json_path is None:
        return APPLIED_PATH
    return Path(all_json_path).parent / "applied.json"


def _load_unlocked(all_json_path=None) -> dict:
    """Return id -> ISO timestamp. Missing file -> {}.

    Corrupt / non-dict primary raises (do not fail-open to {} and wipe on next
    save/merge — same data-loss class as desc_store).
    """
    p = path_for(all_json_path)
    if not p.is_file():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{p} is not a JSON object")
    out = {}
    for k, v in data.items():
        if isinstance(k, str) and k and isinstance(v, str) and v:
            out[k] = v
    return out


def load(all_json_path=None) -> dict:
    """Return id -> ISO timestamp. Missing file -> {}.

    Corrupt / non-dict primary raises (do not fail-open to {}).
    """
    # Reads are atomic relative to os.replace of the final path; no lock needed.
    return _load_unlocked(all_json_path)


def _save_unlocked(marks: dict, all_json_path=None) -> None:
    """Atomically rewrite the full id -> ISO map (caller holds _lock)."""
    clean = {
        k: v for k, v in (marks or {}).items()
        if isinstance(k, str) and k and isinstance(v, str) and v
    }
    p = path_for(all_json_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(clean, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    # Unique tmp name: concurrent writers must not share applied.json.tmp.
    fd, tmp_name = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        Path(tmp_name).replace(p)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def save(marks: dict, all_json_path=None) -> None:
    """Atomically rewrite the full id -> ISO map."""
    with _lock:
        _save_unlocked(marks, all_json_path)


def merge(incoming: dict, all_json_path=None) -> dict:
    """Union with disk: keep the later ISO timestamp per id. Returns merged."""
    with _lock:
        cur = _load_unlocked(all_json_path)
        for k, v in (incoming or {}).items():
            if not isinstance(k, str) or not k or not isinstance(v, str) or not v:
                continue
            if k not in cur or v > cur[k]:
                cur[k] = v
        _save_unlocked(cur, all_json_path)
        return dict(cur)


def selftest():
    import tempfile as tmpmod

    td = Path(tmpmod.mkdtemp())
    all_p = td / "all.json"
    all_p.write_text("[]")
    assert load(all_p) == {}  # missing applied.json next to all.json
    save({"aaa": "2026-01-01T00:00:00Z"}, all_p)
    assert load(all_p) == {"aaa": "2026-01-01T00:00:00Z"}
    m = merge({"aaa": "2025-01-01T00:00:00Z", "bbb": "2026-02-01T00:00:00Z"}, all_p)
    assert m["aaa"] == "2026-01-01T00:00:00Z"  # kept later
    assert m["bbb"] == "2026-02-01T00:00:00Z"

    # replace (save) drops keys not in the payload
    save({"bbb": "2026-02-01T00:00:00Z"}, all_p)
    assert load(all_p) == {"bbb": "2026-02-01T00:00:00Z"}

    # corrupt primary must not fail-open to {} (would wipe on next save/merge)
    applied_p = path_for(all_p)
    applied_p.write_text("not json{{{", encoding="utf-8")
    try:
        load(all_p)
        raise AssertionError("expected error on corrupt applied store")
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    # non-dict primary must raise
    applied_p.write_text("[]", encoding="utf-8")
    try:
        load(all_p)
        raise AssertionError("expected error on non-dict applied store")
    except ValueError:
        pass
    # restore valid so later tests can reuse if needed
    save({"bbb": "2026-02-01T00:00:00Z"}, all_p)

    # concurrent merges must not lose keys or crash on shared tmp
    td2 = Path(tmpmod.mkdtemp())
    all2 = td2 / "all.json"
    all2.write_text("[]")
    errors = []

    def merge_many(start, n):
        try:
            for i in range(start, start + n):
                merge({f"a{i}": f"2026-01-01T00:00:{i % 60:02d}Z"}, all2)
        except Exception as e:  # noqa: BLE001 - collect for assert
            errors.append(e)

    threads = [threading.Thread(target=merge_many, args=(i * 20, 20)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert len(load(all2)) == 100, len(load(all2))

    print("applied_store selftest OK")


if __name__ == "__main__":
    selftest()
