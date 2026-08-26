import os
import sqlite3
from pathlib import Path

ENV_VAR = "JOBCANNON_SOURCE_DB"


def open_readonly(db_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite DB truly read-only (URI mode=ro): writes raise OperationalError."""
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def resolve_source_db() -> Path:
    """Resolve the private source DB path from the environment. Never hardcode it."""
    raw = os.environ.get(ENV_VAR)
    if not raw:
        raise RuntimeError(f"{ENV_VAR} is not set; point it at the private jobs.db")
    path = Path(raw)
    if not path.is_file():
        raise RuntimeError(f"{ENV_VAR}={raw} does not exist")
    return path
