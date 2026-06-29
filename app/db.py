"""SQLite access layer. Single-file DB, no external server."""
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "app.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    email      TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id         INTEGER PRIMARY KEY,
    name       TEXT UNIQUE NOT NULL,
    seats      INTEGER NOT NULL CHECK (seats >= 0),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assignments (
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (user_id, subscription_id)
);

CREATE TABLE IF NOT EXISTS invoices (
    id              INTEGER PRIMARY KEY,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    label           TEXT NOT NULL,
    amount          REAL NOT NULL,
    currency        TEXT,
    due_date        TEXT,
    status          TEXT NOT NULL DEFAULT 'due' CHECK (status IN ('due', 'paid')),
    created_at      TEXT NOT NULL,
    paid_at         TEXT
);

CREATE TABLE IF NOT EXISTS custom_tabs (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS custom_tab_items (
    tab_id      INTEGER NOT NULL REFERENCES custom_tabs(id) ON DELETE CASCADE,
    section_key TEXT NOT NULL,
    position    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tab_id, section_key)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_iso() -> str:
    return date.today().isoformat()


def get_conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive, idempotent column adds for upgrades from v1."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(subscriptions)")}
    if "unit_cost" not in cols:
        conn.execute("ALTER TABLE subscriptions ADD COLUMN unit_cost REAL")
    if "currency" not in cols:
        conn.execute("ALTER TABLE subscriptions ADD COLUMN currency TEXT")


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


# --- settings key/value -----------------------------------------------------
def get_setting(key: str, default: str = "") -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_setting(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def all_settings() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}
