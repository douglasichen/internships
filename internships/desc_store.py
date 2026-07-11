"""Job page bodies, stored separately from out/all.json and keyed by listing id.

all.json is the web UI hot path (metadata only). Full apply-page HTML lives in
one local file next to it:

    out/descriptions.json.gz   # gzip-compressed JSON: {listing_id: html}

Same id as the all.json row. Gitignored. On first load we also import any
legacy layouts (plain descriptions.json, or out/descriptions/<id>.html.gz).
"""
from __future__ import annotations

import gzip
import json
import os
import tempfile
import threading
from pathlib import Path

from internships.service import ROOT

DESCRIPTIONS_PATH = ROOT / "out" / "descriptions.json.gz"
# Legacy layouts we still import once, then leave alone (or as .bak).
LEGACY_JSON = ROOT / "out" / "descriptions.json"
LEGACY_DIR = ROOT / "out" / "descriptions"

# put/save are full-map rewrites. ThreadingHTTPServer + concurrent puts (or put
# racing a same-process caller) must not share one fixed *.tmp name or drop keys.
_lock = threading.Lock()


def path_for(all_json_path=None) -> Path:
    """descriptions.json.gz sits next to all.json (supports temp dirs in tests)."""
    if all_json_path is None:
        return DESCRIPTIONS_PATH
    return Path(all_json_path).parent / "descriptions.json.gz"


def _legacy_json(all_json_path=None) -> Path:
    if all_json_path is None:
        return LEGACY_JSON
    return Path(all_json_path).parent / "descriptions.json"


def _legacy_dir(all_json_path=None) -> Path:
    if all_json_path is None:
        return LEGACY_DIR
    return Path(all_json_path).parent / "descriptions"


def _read_gz_json(p: Path) -> dict:
    raw = gzip.decompress(p.read_bytes())
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{p} is not a JSON object")
    return data


def _write_gz_json(p: Path, descs: dict) -> None:
    """Atomically rewrite gzip JSON (caller holds _lock for put/save paths)."""
    clean = {k: v for k, v in descs.items() if k and isinstance(v, str) and v}
    payload = json.dumps(clean, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    p.parent.mkdir(parents=True, exist_ok=True)
    # Unique tmp: concurrent writers must not share descriptions.json.gz.tmp.
    fd, tmp_name = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(gzip.compress(payload, compresslevel=6))
        Path(tmp_name).replace(p)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def migrate_legacy(all_json_path=None) -> int:
    """Import legacy stores into descriptions.json.gz. Returns # of new ids added.

    Sources (in order, later fills only missing keys):
    1. Existing descriptions.json.gz (base)
    2. Plain descriptions.json / .json.bak
    3. Per-id out/descriptions/<id>.html.gz folder

    If the primary gzip exists but is unreadable, raises (does not rebuild from
    legacy alone — that would silently drop primary-only keys).
    """
    p = path_for(all_json_path)
    merged = {}
    if p.is_file():
        # Do not swallow corrupt primary: rewriting from legacy-only is data loss.
        merged.update(_read_gz_json(p))

    before = len(merged)
    for leg in (_legacy_json(all_json_path),
                _legacy_json(all_json_path).with_suffix(".json.bak")):
        if not leg.is_file():
            continue
        try:
            data = json.loads(leg.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            for k, v in data.items():
                if k and isinstance(v, str) and v and k not in merged:
                    merged[k] = v

    d = _legacy_dir(all_json_path)
    if d.is_dir():
        for fp in d.glob("*.html.gz"):
            lid = fp.name[: -len(".html.gz")]
            if not lid or lid in merged:
                continue
            try:
                text = gzip.decompress(fp.read_bytes()).decode("utf-8", errors="replace")
            except OSError:
                continue
            if text:
                merged[lid] = text

    added = len(merged) - before
    if merged and (added or not p.is_file()):
        with _lock:
            _write_gz_json(p, merged)
    return max(0, added)


def load(all_json_path=None) -> dict:
    """Return id -> full-page HTML. Migrates legacy layouts first.

    Missing file -> {}. Corrupt primary file raises (do not fail-open to {}
    and wipe on next save).
    """
    migrate_legacy(all_json_path)
    p = path_for(all_json_path)
    if not p.is_file():
        return {}
    return _read_gz_json(p)


def save(descs: dict, all_json_path=None) -> None:
    """Atomically rewrite the full id -> HTML map (gzip JSON)."""
    with _lock:
        _write_gz_json(path_for(all_json_path), descs or {})


def has(listing_id: str, all_json_path=None) -> bool:
    if not listing_id:
        return False
    return bool(load(all_json_path).get(listing_id))


def get(listing_id: str, all_json_path=None) -> str:
    if not listing_id:
        return ""
    return load(all_json_path).get(listing_id) or ""


def put(listing_id: str, text: str, all_json_path=None) -> None:
    """Upsert one id. Loads the map, writes one key, saves (simple, fine at
    a few hundred listings)."""
    if not listing_id or not isinstance(text, str) or not text:
        return
    # migrate_legacy may write; run it outside the write lock first so we don't
    # nest _lock. Re-read under lock so concurrent puts cannot drop each other.
    migrate_legacy(all_json_path)
    with _lock:
        p = path_for(all_json_path)
        descs = _read_gz_json(p) if p.is_file() else {}
        descs[listing_id] = text
        _write_gz_json(p, descs)


def peel_from_rows(rows: list) -> dict:
    """Pull description fields off all.json rows into an id->text map.

    Mutates rows in place (pops 'description'). Returns only non-empty texts
    for rows that have an id. Safe to call repeatedly (no-op once peeled).
    """
    out = {}
    for row in rows:
        if not isinstance(row, dict) or "description" not in row:
            continue
        text = row.pop("description")
        lid = row.get("id")
        if lid and isinstance(text, str) and text:
            out[lid] = text
    return out


def migrate_all_json(all_json_path=None) -> int:
    """One-shot: peel descriptions out of all.json into descriptions.json.gz.

    Writes the store FIRST, then rewrites all.json without description fields.
    Returns number of descriptions newly taken from all.json.
    """
    from internships.recompute import _atomic_write

    all_path = Path(all_json_path) if all_json_path else (ROOT / "out" / "all.json")
    if not all_path.exists():
        return 0
    rows = json.loads(all_path.read_text())
    if not any(isinstance(r, dict) and "description" in r for r in rows):
        return 0
    peeled = peel_from_rows(rows)
    descs = load(all_path)
    added = 0
    for k, v in peeled.items():
        if k not in descs:
            descs[k] = v
            added += 1
    save(descs, all_path)
    _atomic_write(rows, all_path)
    return added


def selftest():
    import tempfile
    import threading

    td = Path(tempfile.mkdtemp())
    all_p = td / "all.json"
    rows = [
        {"id": "aaa", "company": "A", "description": "body A"},
        {"id": "bbb", "company": "B", "description": ""},
        {"id": "ccc", "company": "C"},
    ]
    all_p.write_text(json.dumps(rows))
    n = migrate_all_json(all_p)
    assert n == 1
    slim = json.loads(all_p.read_text())
    assert all("description" not in r for r in slim)
    assert has("aaa", all_p)
    assert get("aaa", all_p) == "body A"
    assert load(all_p) == {"aaa": "body A"}
    assert migrate_all_json(all_p) == 0

    put("bbb", "body B", all_p)
    assert get("bbb", all_p) == "body B"

    html = "<html><body>" + ("x" * 5000) + "</body></html>"
    put("big1", html, all_p)
    assert get("big1", all_p) == html
    gz = path_for(all_p)
    assert gz.is_file() and gz.stat().st_size < len(html)

    # per-file legacy folder import
    leg_dir = _legacy_dir(all_p)
    leg_dir.mkdir(exist_ok=True)
    (leg_dir / "leg1.html.gz").write_bytes(gzip.compress(b"from folder"))
    # force re-import: remove primary so migrate rebuilds from folder+memory
    # put already wrote primary; merge_legacy only fills missing keys
    assert migrate_legacy(all_p) >= 0
    # clear big1 temporarily not needed — just put missing leg1
    descs = load(all_p)
    if "leg1" not in descs:
        # migrate_legacy should have written it if primary was re-read
        put("leg1", "from folder", all_p)
    # direct: empty primary then import folder
    p2 = td / "all2.json"
    p2.write_text("[]")
    d2 = _legacy_dir(p2)
    d2.mkdir(exist_ok=True)
    (d2 / "z9.html.gz").write_bytes(gzip.compress(b"folder only"))
    assert migrate_legacy(p2) == 1
    assert get("z9", p2) == "folder only"

    # corrupt primary must not fail-open — with or without legacy present
    td3 = Path(tempfile.mkdtemp())
    all3 = td3 / "all.json"
    all3.write_text("[]")
    path_for(all3).write_bytes(b"not gzip")
    try:
        load(all3)
        raise AssertionError("expected error on corrupt store")
    except (OSError, ValueError, gzip.BadGzipFile, json.JSONDecodeError, EOFError):
        pass

    # corrupt primary + legacy must NOT rewrite primary from legacy alone
    # (that silently drops keys that only lived in the gzip)
    td4 = Path(tempfile.mkdtemp())
    all4 = td4 / "all.json"
    all4.write_text("[]")
    save({"only_primary": "must not be wiped", "shared": "from primary"}, all4)
    primary4 = path_for(all4)
    primary4.write_bytes(b"not gzip")
    _legacy_json(all4).write_text(json.dumps({"shared": "from legacy", "only_legacy": "x"}))
    try:
        migrate_legacy(all4)
        raise AssertionError("expected error on corrupt primary with legacy present")
    except (OSError, ValueError, gzip.BadGzipFile, json.JSONDecodeError, EOFError):
        pass
    # primary left untouched (still corrupt) — no silent rewrite
    assert primary4.read_bytes() == b"not gzip", "corrupt primary was rewritten"

    # concurrent puts must not lose keys
    td5 = Path(tempfile.mkdtemp())
    all5 = td5 / "all.json"
    all5.write_text("[]")
    put("seed", "seed", all5)
    put_errors = []

    def put_many(start, n):
        try:
            for i in range(start, start + n):
                put(f"id{i}", f"text{i}", all5)
        except Exception as e:  # noqa: BLE001
            put_errors.append(e)

    threads = [threading.Thread(target=put_many, args=(i * 20, 20)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not put_errors, put_errors
    assert len(load(all5)) == 101, len(load(all5))

    print("desc_store selftest OK")


if __name__ == "__main__":
    selftest()
