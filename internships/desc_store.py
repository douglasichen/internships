"""Job descriptions, stored separately from out/all.json and keyed by listing id.

all.json is the web UI's hot path (~metadata only). Descriptions are large HTML
blobs (~40MB for a few hundred jobs) that the UI never renders, so they live in
out/descriptions.json as {id: text}. Same id as the all.json row.
"""
import json
from pathlib import Path

from internships.service import ROOT

DESCRIPTIONS_PATH = ROOT / "out" / "descriptions.json"


def path_for(all_json_path) -> Path:
    """descriptions.json sits next to all.json (supports temp dirs in tests)."""
    return Path(all_json_path).parent / "descriptions.json"


def load(all_json_path=None) -> dict:
    """Return id -> description. Missing file -> {}.

    Corrupt / unreadable existing file raises -- callers must not treat that
    as an empty store and rewrite (would wipe descriptions)."""
    p = path_for(all_json_path) if all_json_path else DESCRIPTIONS_PATH
    if not p.exists():
        return {}
    data = json.loads(p.read_text())  # ValueError/OSError propagate
    if not isinstance(data, dict):
        raise ValueError(f"{p} is not a JSON object")
    return data


def save(descs: dict, all_json_path=None) -> None:
    """Atomic write of the full id -> description map."""
    from internships.recompute import _atomic_write

    p = path_for(all_json_path) if all_json_path else DESCRIPTIONS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    # only persist non-empty strings
    clean = {k: v for k, v in descs.items() if k and isinstance(v, str) and v}
    _atomic_write(clean, p)


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
    """One-shot: peel descriptions out of all.json into descriptions.json.

    Writes descriptions.json FIRST (merged with any existing store), then
    rewrites all.json without description fields -- so a crash between the
    two leaves bodies safe (and all.json still peelable on retry).

    Returns number of descriptions newly taken from all.json. No-op if
    all.json has no description fields.
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
        # if already present, keep the existing store value (don't clobber)
    # store first, then slim all.json
    save(descs, all_path)
    _atomic_write(rows, all_path)
    return added


def selftest():
    import tempfile
    from pathlib import Path

    td = Path(tempfile.mkdtemp())
    all_p = td / "all.json"
    rows = [
        {"id": "aaa", "company": "A", "description": "body A"},
        {"id": "bbb", "company": "B", "description": ""},
        {"id": "ccc", "company": "C"},  # no description key
    ]
    all_p.write_text(json.dumps(rows))
    n = migrate_all_json(all_p)
    assert n == 1  # only non-empty body A
    slim = json.loads(all_p.read_text())
    assert all("description" not in r for r in slim)
    descs = load(all_p)
    assert descs == {"aaa": "body A"}

    # second migrate is a no-op
    assert migrate_all_json(all_p) == 0
    assert load(all_p) == {"aaa": "body A"}

    # append-style save merge
    descs["bbb"] = "body B"
    save(descs, all_p)
    assert load(all_p)["bbb"] == "body B"

    # empty strings dropped on save
    descs["ccc"] = ""
    save(descs, all_p)
    assert "ccc" not in load(all_p)

    # corrupt store must not fail-open to {}
    bad = path_for(all_p)
    bad.write_text("NOT JSON{{{")
    try:
        load(all_p)
        raise AssertionError("corrupt store should raise")
    except ValueError:
        pass

    print("desc_store selftest OK")


if __name__ == "__main__":
    selftest()
