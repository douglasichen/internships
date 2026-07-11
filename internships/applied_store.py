"""Manual "applied" marks: id -> ISO timestamp, stored next to all.json.

Browser localStorage alone is fragile (localhost vs 127.0.0.1 are different
origins). This file is the durable source of truth on disk; the FE still
mirrors into localStorage for offline/static opens.
"""
from __future__ import annotations

import json
from pathlib import Path

from internships.service import ROOT

APPLIED_PATH = ROOT / "out" / "applied.json"


def path_for(all_json_path=None) -> Path:
    if all_json_path is None:
        return APPLIED_PATH
    return Path(all_json_path).parent / "applied.json"


def load(all_json_path=None) -> dict:
    """Return id -> ISO timestamp. Missing/empty file -> {}."""
    p = path_for(all_json_path)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for k, v in data.items():
        if isinstance(k, str) and k and isinstance(v, str) and v:
            out[k] = v
    return out


def save(marks: dict, all_json_path=None) -> None:
    """Atomically rewrite the full id -> ISO map."""
    clean = {
        k: v for k, v in (marks or {}).items()
        if isinstance(k, str) and k and isinstance(v, str) and v
    }
    p = path_for(all_json_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(clean, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(p)


def merge(incoming: dict, all_json_path=None) -> dict:
    """Union with disk: keep the later ISO timestamp per id. Returns merged."""
    cur = load(all_json_path)
    for k, v in (incoming or {}).items():
        if not isinstance(k, str) or not k or not isinstance(v, str) or not v:
            continue
        if k not in cur or v > cur[k]:
            cur[k] = v
    save(cur, all_json_path)
    return cur


def selftest():
    import tempfile

    td = Path(tempfile.mkdtemp())
    all_p = td / "all.json"
    all_p.write_text("[]")
    assert load(all_p) == {}
    save({"aaa": "2026-01-01T00:00:00Z"}, all_p)
    assert load(all_p) == {"aaa": "2026-01-01T00:00:00Z"}
    m = merge({"aaa": "2025-01-01T00:00:00Z", "bbb": "2026-02-01T00:00:00Z"}, all_p)
    assert m["aaa"] == "2026-01-01T00:00:00Z"  # kept later
    assert m["bbb"] == "2026-02-01T00:00:00Z"
    print("applied_store selftest OK")


if __name__ == "__main__":
    selftest()
