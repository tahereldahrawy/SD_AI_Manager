"""End-to-end smoke test. Uses a throwaway DB so it never touches real data."""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Point the app at a temp data dir BEFORE importing app modules.
tmp = Path(tempfile.mkdtemp())
import app.db as db  # noqa: E402

db.DATA_DIR = tmp
db.DB_PATH = tmp / "app.db"

from fastapi.testclient import TestClient  # noqa: E402
import app.main as main  # noqa: E402

main.DATA_DIR = tmp
db.init_db()

from app.auth import create_account  # noqa: E402

create_account("tester", "secret123")

# https base_url so Secure session cookies are stored & resent
c = TestClient(main.app, base_url="https://testserver")

ok = 0
fail = 0


def check(label, cond):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {label}")
    else:
        fail += 1
        print(f"  FAIL  {label}")


# unauthenticated -> redirected to login
r = c.get("/", follow_redirects=False)
check("anon hitting / is redirected", r.status_code == 303 and r.headers["location"] == "/login")

# bad login
r = c.post("/login", data={"username": "tester", "password": "wrong"}, follow_redirects=False)
check("bad password rejected", r.status_code == 303 and r.headers["location"] == "/login")

# good login
r = c.post("/login", data={"username": "tester", "password": "secret123"}, follow_redirects=False)
check("good login redirects to /", r.status_code == 303 and r.headers["location"] == "/")

# create user
r = c.post("/users", data={"name": "Alice", "email": "Alice@Corp.com"}, follow_redirects=True)
check("create user ok", r.status_code == 200 and "alice@corp.com" in r.text)

# duplicate email (case-insensitive) rejected
r = c.post("/users", data={"name": "Alice2", "email": "alice@corp.com"}, follow_redirects=True)
check("duplicate email rejected", "already exists" in r.text)

c.post("/users", data={"name": "Bob", "email": "bob@corp.com"})

# create subscription with 1 seat
r = c.post("/subscriptions", data={"name": "Pro Plan", "seats": "1"}, follow_redirects=True)
check("create subscription ok", "Pro Plan" in r.text)

# duplicate name rejected
r = c.post("/subscriptions", data={"name": "Pro Plan", "seats": "5"}, follow_redirects=True)
check("duplicate subscription rejected", "already exists" in r.text)

# find ids
with db.get_conn() as conn:
    sub_id = conn.execute("SELECT id FROM subscriptions WHERE name='Pro Plan'").fetchone()[0]
    alice = conn.execute("SELECT id FROM users WHERE email='alice@corp.com'").fetchone()[0]
    bob = conn.execute("SELECT id FROM users WHERE email='bob@corp.com'").fetchone()[0]

# assign Alice (fills the 1 seat)
r = c.post(f"/subscriptions/{sub_id}/assign", data={"user_id": alice}, follow_redirects=True)
check("assign within seats ok", "User assigned" in r.text)

# assign Bob -> no free seats
r = c.post(f"/subscriptions/{sub_id}/assign", data={"user_id": bob}, follow_redirects=True)
check("over-assign blocked", "No free seats" in r.text)

# shrink seats below consumed -> blocked
r = c.post(f"/subscriptions/{sub_id}/seats", data={"seats": "0"}, follow_redirects=True)
check("shrink below consumed blocked", "already consumed" in r.text)

# grow seats to 2, then Bob fits
c.post(f"/subscriptions/{sub_id}/seats", data={"seats": "2"})
r = c.post(f"/subscriptions/{sub_id}/assign", data={"user_id": bob}, follow_redirects=True)
check("assign after growing seats ok", "User assigned" in r.text)

# exports
r = c.get("/export?kind=users&fmt=csv")
check("export users csv", r.status_code == 200 and b"alice@corp.com" in r.content)
r = c.get("/export?kind=subscriptions&fmt=xlsx")
check("export subs xlsx", r.status_code == 200 and r.content[:2] == b"PK")
r = c.get("/export?kind=all&fmt=csv")
check("export all csv -> zip", r.status_code == 200 and r.content[:2] == b"PK")
r = c.get("/export?kind=all&fmt=xlsx")
check("export all xlsx", r.status_code == 200 and r.content[:2] == b"PK")

# --- accounts (login users) CRUD via GUI ---
# create new account
r = c.post("/accounts", data={"username": "alice_admin", "password": "pw123456"}, follow_redirects=True)
check("create account ok", "created" in r.text and "alice_admin" in r.text)

# duplicate username rejected
r = c.post("/accounts", data={"username": "alice_admin", "password": "pw123456"}, follow_redirects=True)
check("duplicate account rejected", "already exists" in r.text)

# short password rejected
r = c.post("/accounts", data={"username": "shorty", "password": "x"}, follow_redirects=True)
check("short password rejected", "too short" in r.text)

with db.get_conn() as conn:
    alice_acct = conn.execute("SELECT id FROM accounts WHERE username='alice_admin'").fetchone()[0]
    me_acct = conn.execute("SELECT id FROM accounts WHERE username='tester'").fetchone()[0]

# rename + change password
r = c.post(f"/accounts/{alice_acct}/update",
           data={"username": "alice_renamed", "password": "newpw123"}, follow_redirects=True)
check("update account ok", "updated" in r.text and "alice_renamed" in r.text)

# new password actually works (login as renamed account, then restore tester session)
r = c.post("/login", data={"username": "alice_renamed", "password": "newpw123"}, follow_redirects=False)
check("login with changed password works", r.status_code == 303 and r.headers["location"] == "/")
c.post("/login", data={"username": "tester", "password": "secret123"})  # back to tester

# cannot delete self
r = c.post(f"/accounts/{me_acct}/delete", follow_redirects=True)
check("self-delete blocked", "cannot delete the account you are logged in as" in r.text.lower())

# delete other account ok
r = c.post(f"/accounts/{alice_acct}/delete", follow_redirects=True)
check("delete other account ok", "deleted" in r.text)

# cannot delete last remaining account
r = c.post(f"/accounts/{me_acct}/delete", follow_redirects=True)
check("last-account-delete blocked",
      ("cannot delete the last account" in r.text.lower())
      or ("cannot delete the account you are logged in as" in r.text.lower()))

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
