"""On-disk persistence for the Research Store (file-memory backend).

  research_store/current.json        the BELIEF   — current research product
  research_store/archive/<ts>.json   history      — every product, timestamped
  research_store/journal.jsonl       the JOURNAL  — append-only run/outcome log

Atomic writes (temp file + os.replace). Plain JSON, model-independent. Runtime
state — git-ignored, regenerated on the box.

NOTE: this is the repo-root `research_store/` DATA directory, which is distinct
from the `src/research_store/` CODE package importing it. Different paths (the
data dir is not on sys.path), so there is no import collision.
"""
import json
import os
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
STORE_DIR = REPO_ROOT / "research_store"
CURRENT = STORE_DIR / "current.json"
ARCHIVE = STORE_DIR / "archive"
JOURNAL = STORE_DIR / "journal.jsonl"


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def save_current(product_dict: dict, *, archive: bool = True) -> Path:
    """Atomically write the current product; optionally archive a dated copy."""
    _atomic_write(CURRENT, product_dict)
    if archive:
        stamp = str(product_dict.get("as_of", "")).replace(":", "-") or "unstamped"
        _atomic_write(ARCHIVE / f"{stamp}.json", product_dict)
    return CURRENT


def load_current() -> dict | None:
    """Return the current product dict, or None if none written yet."""
    if not CURRENT.exists():
        return None
    with CURRENT.open() as f:
        return json.load(f)


def append_journal(entry: dict) -> None:
    """Append one event to the journal (one JSON object per line)."""
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def read_journal() -> list:
    """Return the full journal (list of event dicts; [] if none)."""
    if not JOURNAL.exists():
        return []
    out = []
    with JOURNAL.open() as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out
