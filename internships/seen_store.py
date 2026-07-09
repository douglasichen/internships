"""Tracks which listing ids have already been reported, per source, so a run
only returns what's new since the last run. Replaces the old full-snapshot
diff (responses/ + reports/) with a much smaller "have I seen this id" set."""
import json
from pathlib import Path


class SeenStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> set:
        if not self.path.exists():
            return set()
        return set(json.loads(self.path.read_text()))

    def save(self, ids: set):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(ids)))
        tmp.replace(self.path)


def selftest():
    import tempfile
    p = Path(tempfile.mkdtemp()) / "seen.json"
    store = SeenStore(p)
    assert store.load() == set()
    store.save({"a", "b"})
    assert store.load() == {"a", "b"}
    print("seen_store selftest OK")


if __name__ == "__main__":
    selftest()
