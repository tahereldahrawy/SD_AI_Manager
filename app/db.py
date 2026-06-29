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

CREATE TABLE IF NOT EXISTS logs (
    id      INTEGER PRIMARY KEY,
    ts      TEXT NOT NULL,
    account TEXT,
    action  TEXT NOT NULL,
    detail  TEXT,
    status  INTEGER
);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY,
    label      TEXT NOT NULL,
    match_path TEXT NOT NULL,
    recipient  TEXT,
    enabled    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""

# Built-in notification rules, seeded once (disabled by default — user opts in).
NOTIFY_SEED = [
    ("Bill paid", "/invoices/*/paid"),
    ("Bill created", "/invoices"),
    ("Subscription created", "/subscriptions"),
    ("User created", "/users"),
    ("Account created", "/accounts"),
]


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
    if "daily_basis" not in cols:
        conn.execute("ALTER TABLE subscriptions ADD COLUMN daily_basis INTEGER NOT NULL DEFAULT 0")


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        _seed_notifications(conn)


def _seed_notifications(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0:
        for label, patt in NOTIFY_SEED:
            conn.execute(
                "INSERT INTO notifications (label, match_path, recipient, enabled, created_at) "
                "VALUES (?, ?, '', 0, ?)",
                (label, patt, now_iso()),
            )


# --- notification rules -----------------------------------------------------
def list_notifications():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM notifications ORDER BY id").fetchall()


def enabled_notifications():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM notifications WHERE enabled = 1").fetchall()


def add_notification(label: str, match_path: str, recipient: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO notifications (label, match_path, recipient, enabled, created_at) "
            "VALUES (?, ?, ?, 1, ?)",
            (label, match_path, recipient, now_iso()),
        )


def update_notification(nid: int, label: str, match_path: str, recipient: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE notifications SET label = ?, match_path = ?, recipient = ? WHERE id = ?",
            (label, match_path, recipient, nid),
        )


def set_notification_enabled(nid: int, enabled: bool) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE notifications SET enabled = ? WHERE id = ?",
                     (1 if enabled else 0, nid))


def delete_notification(nid: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM notifications WHERE id = ?", (nid,))


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


def add_log(account, action: str, detail: str = "", status=None) -> None:
    """Record one action in the system log. Never raises (audit must not break a request)."""
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO logs (ts, account, action, detail, status) VALUES (?, ?, ?, ?, ?)",
                (now_iso(), account, action, detail, status),
            )
    except Exception:
        pass


def clear_logs() -> int:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM logs")
        return cur.rowcount


def all_settings() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}
