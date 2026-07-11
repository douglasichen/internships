"""Job page bodies, stored separately from out/all.json and keyed by listing id.

all.json is the web UI hot path (metadata only). Full apply-page HTML for each
listing lives under out/descriptions/ as one gzip-compressed file per id:

    out/descriptions/<listing_id>.html.gz

Same id as the all.json row. The directory is gitignored (local-only). A
legacy out/descriptions.json map is migrated into the folder on first load.
"""
from __future__ import annotations

import gzip
import json
import re
from pathlib import Path

from internships.service import ROOT

DESCRIPTIONS_DIR = ROOT / "out" / "descriptions"
LEGACY_JSON = ROOT / "out" / "descriptions.json"
_ID_SAFE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def dir_for(all_json_path=None) -> Path:
    """descriptions/ sits next to all.json (supports temp dirs in tests)."""
    if all_json_path is None:
        return DESCRIPTIONS_DIR
    return Path(all_json_path).parent / "descriptions"


def legacy_json_for(all_json_path=None) -> Path:
    if all_json_path is None:
        return LEGACY_JSON
    return Path(all_json_path).parent / "descriptions.json"


def _gz_path(listing_id: str, all_json_path=None) -> Path:
    if not listing_id or not _ID_SAFE.match(listing_id):
        raise ValueError(f"unsafe listing id for description path: {listing_id!r}")
    return dir_for(all_json_path) / f"{listing_id}.html.gz"


def has(listing_id: str, all_json_path=None) -> bool:
    """True if a non-empty gzip body exists for this id."""
    try:
        p = _gz_path(listing_id, all_json_path)
    except ValueError:
        return False
    return p.is_file() and p.stat().st_size > 0


def get(listing_id: str, all_json_path=None) -> str:
    """Return decompressed HTML/text for id, or '' if missing."""
    try:
        p = _gz_path(listing_id, all_json_path)
    except ValueError:
        return ""
    if not p.is_file():
        return ""
    try:
        return gzip.decompress(p.read_bytes()).decode("utf-8", errors="replace")
    except OSError:
        return ""


def put(listing_id: str, text: str, all_json_path=None) -> None:
    """Write one gzip-compressed page body. No-op for empty id/text."""
    if not listing_id or not isinstance(text, str) or not text:
        return
    p = _gz_path(listing_id, all_json_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    raw = gzip.compress(text.encode("utf-8"), compresslevel=6)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_bytes(raw)
    tmp.replace(p)


def load(all_json_path=None) -> dict:
    """Return id -> text for every stored description.

    Migrates legacy descriptions.json into the gzip folder first (if present).
    """
    migrate_legacy_json(all_json_path)
    d = dir_for(all_json_path)
    out = {}
    if not d.is_dir():
        return out
    for p in d.glob("*.html.gz"):
        lid = p.name[: -len(".html.gz")]
        if not _ID_SAFE.match(lid):
            continue
        try:
            text = gzip.decompress(p.read_bytes()).decode("utf-8", errors="replace")
        except OSError:
            continue
        if text:
            out[lid] = text
    return out


def save(descs: dict, all_json_path=None) -> None:
    """Upsert all non-empty entries. Does not delete ids absent from descs."""
    for k, v in (descs or {}).items():
        if k and isinstance(v, str) and v:
            put(k, v, all_json_path)


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


def migrate_legacy_json(all_json_path=None) -> int:
    """Move legacy out/descriptions.json into out/descriptions/<id>.html.gz.

    Returns number of files written. Renames the JSON to .bak after success.
    """
    leg = legacy_json_for(all_json_path)
    if not leg.is_file():
        return 0
    try:
        data = json.loads(leg.read_text())
    except (ValueError, OSError):
        return 0
    if not isinstance(data, dict):
        return 0
    n = 0
    for k, v in data.items():
        if k and isinstance(v, str) and v and not has(k, all_json_path):
            put(k, v, all_json_path)
            n += 1
    bak = leg.with_suffix(".json.bak")
    try:
        leg.replace(bak)
    except OSError:
        pass
    return n


def migrate_all_json(all_json_path=None) -> int:
    """One-shot: peel descriptions out of all.json into the gzip store.

    Writes bodies FIRST, then rewrites all.json without description fields.
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
    added = 0
    for k, v in peeled.items():
        if not has(k, all_path):
            put(k, v, all_path)
            added += 1
    _atomic_write(rows, all_path)
    return added


def selftest():
    import tempfile

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
    # gzip round-trip of larger HTML
    html = "<html><body>" + ("x" * 5000) + "<script>alert(1)</script></body></html>"
    put("big1", html, all_p)
    assert get("big1", all_p) == html
    gz = dir_for(all_p) / "big1.html.gz"
    assert gz.is_file() and gz.stat().st_size < len(html)

    # legacy JSON migration
    leg = legacy_json_for(all_p)
    leg.write_text(json.dumps({"leg1": "from json"}))
    assert migrate_legacy_json(all_p) == 1
    assert get("leg1", all_p) == "from json"
    assert not leg.exists()

    # unsafe id rejected
    try:
        put("../evil", "x", all_p)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass

    print("desc_store selftest OK")


if __name__ == "__main__":
    selftest()
