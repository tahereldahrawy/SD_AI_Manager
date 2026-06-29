"""Password hashing + account lookup. pbkdf2_sha256 = pure-python, no native build."""
from passlib.hash import pbkdf2_sha256

from .db import get_conn, now_iso


def hash_password(plain: str) -> str:
    return pbkdf2_sha256.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pbkdf2_sha256.verify(plain, hashed)
    except (ValueError, TypeError):
        return False


def create_account(username: str, password: str) -> int:
    username = username.strip()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO accounts (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, hash_password(password), now_iso()),
        )
        return cur.lastrowid


def authenticate(username: str, password: str):
    """Return account row on success, else None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM accounts WHERE username = ?", (username.strip(),)
        ).fetchone()
    if row and verify_password(password, row["password_hash"]):
        return row
    return None
