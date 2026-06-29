"""Create / reset a login account (the people who sign in to the app).

Usage:
    python scripts/create_account.py <username>

Prompts for a password (hidden). If the username exists, resets its password.
"""
import getpass
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import hash_password  # noqa: E402
from app.db import get_conn, init_db, now_iso  # noqa: E402


def main():
    if len(sys.argv) != 2:
        print("Usage: python scripts/create_account.py <username>")
        sys.exit(1)
    username = sys.argv[1].strip()
    if not username:
        print("Username cannot be empty.")
        sys.exit(1)

    pw1 = getpass.getpass("Password: ")
    pw2 = getpass.getpass("Confirm:  ")
    if pw1 != pw2:
        print("Passwords do not match.")
        sys.exit(1)
    if len(pw1) < 6:
        print("Password too short (min 6 chars).")
        sys.exit(1)

    init_db()
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO accounts (username, password_hash, created_at) "
                "VALUES (?, ?, ?)",
                (username, hash_password(pw1), now_iso()),
            )
            print(f"Account '{username}' created.")
        except sqlite3.IntegrityError:
            conn.execute(
                "UPDATE accounts SET password_hash = ? WHERE username = ?",
                (hash_password(pw1), username),
            )
            print(f"Account '{username}' password reset.")


if __name__ == "__main__":
    main()
