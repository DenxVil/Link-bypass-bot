import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


DB_PATH = os.getenv("SQLITE_PATH", "/tmp/link-resolver.sqlite3")
CACHE_TTL_SECONDS = max(60, int(os.getenv("CACHE_TTL_SECONDS", "86400")))
RATE_LIMIT_COUNT = max(1, int(os.getenv("RATE_LIMIT_COUNT", "40")))
RATE_LIMIT_WINDOW = max(60, int(os.getenv("RATE_LIMIT_WINDOW", "3600")))


def _connect():
    path = Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def _db():
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_storage():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS resolutions (
                source TEXT PRIMARY KEY,
                destination TEXT NOT NULL,
                hops INTEGER NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rate_events (
                user_id INTEGER NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rate_user_time ON rate_events(user_id, created_at)"
        )


def cache_key(url: str) -> str:
    parts = urlsplit(url.strip())
    # Versioned key invalidates destinations saved by the earlier, over-eager
    # browser resolver without deleting rate-limit history.
    return "resolver-v2:" + urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, "")
    )


def get_cached(source: str):
    cutoff = time.time() - CACHE_TTL_SECONDS
    with _db() as conn:
        row = conn.execute(
            "SELECT destination, hops FROM resolutions WHERE source=? AND created_at>=?",
            (cache_key(source), cutoff),
        ).fetchone()
        if row:
            return row[0], row[1]
        conn.execute("DELETE FROM resolutions WHERE created_at<?", (cutoff,))
    return None


def put_cached(source: str, destination: str, hops: int):
    with _db() as conn:
        conn.execute(
            """INSERT INTO resolutions(source, destination, hops, created_at)
               VALUES(?, ?, ?, ?)
               ON CONFLICT(source) DO UPDATE SET
                 destination=excluded.destination,
                 hops=excluded.hops,
                 created_at=excluded.created_at""",
            (cache_key(source), destination, int(hops), time.time()),
        )


def allow_request(user_id: int) -> tuple[bool, int]:
    now = time.time()
    cutoff = now - RATE_LIMIT_WINDOW
    with _db() as conn:
        conn.execute("DELETE FROM rate_events WHERE created_at<?", (cutoff,))
        count = conn.execute(
            "SELECT COUNT(*) FROM rate_events WHERE user_id=? AND created_at>=?",
            (user_id, cutoff),
        ).fetchone()[0]
        if count >= RATE_LIMIT_COUNT:
            oldest = conn.execute(
                "SELECT MIN(created_at) FROM rate_events WHERE user_id=? AND created_at>=?",
                (user_id, cutoff),
            ).fetchone()[0]
            retry_after = max(1, int(RATE_LIMIT_WINDOW - (now - oldest)))
            return False, retry_after
        conn.execute("INSERT INTO rate_events(user_id, created_at) VALUES(?, ?)", (user_id, now))
        return True, 0
