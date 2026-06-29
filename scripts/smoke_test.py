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

# rename subscription
r = c.post(f"/subscriptions/{sub_id}/rename", data={"name": "Pro Plan Renamed"}, follow_redirects=True)
check("rename subscription ok", "Pro Plan Renamed" in r.text)
r = c.post(f"/subscriptions/{sub_id}/rename", data={"name": "Pro Plan Renamed"}, follow_redirects=True)
check("rename to same name ok (no duplicate)", "already exists" not in r.text)
# create second sub to test duplicate rejection
c.post("/subscriptions", data={"name": "Other Plan", "seats": "1"})
r = c.post(f"/subscriptions/{sub_id}/rename", data={"name": "Other Plan"}, follow_redirects=True)
check("rename to existing name blocked", "already exists" in r.text)
# restore original name for rest of tests
c.post(f"/subscriptions/{sub_id}/rename", data={"name": "Pro Plan"})

# --- v2: cost, invoices, custom tabs, reminders, settings ---
# set per-seat pricing on Pro Plan (2 users assigned -> charge 20.00 USD)
r = c.post(f"/subscriptions/{sub_id}/pricing",
           data={"unit_cost": "10", "currency": "USD"}, follow_redirects=True)
check("set pricing ok", "Pricing updated" in r.text)
r = c.get(f"/subscriptions/{sub_id}")
check("current charge computed (10 x 2)", "20.00 USD" in r.text)

# daily-basis proration math (deterministic; user example: $25, 10 days, June -> 8.33)
from app.core import compute_charge  # noqa: E402
check("daily proration 25/30*10 = 8.33",
      round(compute_charge(25, 1, True, "2026-06-11", "2026-06-01"), 2) == 8.33)
check("daily proration scales with seats",
      round(compute_charge(25, 2, True, "2026-06-11", "2026-06-01"), 2) == 16.67)
check("non-daily ignores due date (full charge)",
      compute_charge(25, 1, False, "2026-06-11", "2026-06-01") == 25)
check("daily with no upcoming due -> full charge",
      compute_charge(25, 1, True, None, "2026-06-01") == 25)
check("daily past-due date -> full charge",
      compute_charge(25, 1, True, "2026-05-01", "2026-06-01") == 25)

# create invoice with blank amount -> defaults to current charge (20.00)
r = c.post("/invoices",
           data={"subscription_id": sub_id, "label": "June 2026", "due_date": "2020-01-01", "amount": ""},
           follow_redirects=True)
check("create invoice ok", "June 2026" in r.text and "20.00 USD" in r.text)
check("overdue past-due flagged", "overdue" in r.text)

with db.get_conn() as conn:
    inv_id = conn.execute("SELECT id FROM invoices WHERE label='June 2026'").fetchone()[0]
    due_n = conn.execute("SELECT COUNT(*) FROM invoices WHERE status='due'").fetchone()[0]
check("invoice starts due", due_n == 1)

# mark paid, then reopen
r = c.post(f"/invoices/{inv_id}/paid", follow_redirects=True)
check("mark paid ok", "marked paid" in r.text.lower())
with db.get_conn() as conn:
    st = conn.execute("SELECT status FROM invoices WHERE id=?", (inv_id,)).fetchone()[0]
check("status is paid", st == "paid")
r = c.post(f"/invoices/{inv_id}/due", follow_redirects=True)
check("reopen ok", "reverted to due" in r.text.lower())

# dashboard badge counts due invoices
r = c.get("/")
check("dashboard shows bills-due stat", "Bills due" in r.text)

# send reminder with no SMTP configured -> friendly error
r = c.post("/reminders/send", follow_redirects=True)
check("reminder needs SMTP config", "SMTP host and recipient" in r.text)

# custom tab (summary of users + invoices)
r = c.post("/tabs", data={"name": "Overview", "section_users": "1", "section_invoices": "1"},
           follow_redirects=True)
check("create custom tab ok", "Overview" in r.text and "login accounts" not in r.text)
check("custom tab summarizes users", "users" in r.text)
with db.get_conn() as conn:
    tab_id = conn.execute("SELECT id FROM custom_tabs WHERE name='Overview'").fetchone()[0]
r = c.get("/")  # sidebar should now list the custom tab
check("custom tab appears in sidebar", "/tabs/%d" % tab_id in r.text)

# export invoices
r = c.get("/export?kind=invoices&fmt=csv")
check("export invoices csv", r.status_code == 200 and b"June 2026" in r.content)
r = c.get("/export?kind=all&fmt=xlsx")
check("export all still works", r.status_code == 200 and r.content[:2] == b"PK")

# reminder template rendering
from app.core import build_reminder_body, _render_tpl  # noqa: E402
fake_rows = [type("R", (), {"__getitem__": lambda s, k: {"sub": "Acme", "label": "Jul 2026",
    "amount": 25.0, "currency": "USD", "due_date": "2026-07-01"}[k]})()]
body = build_reminder_body(fake_rows, "2026-06-01")
check("default body contains subscription name", "Acme" in body)
check("default body contains amount", "25.00 USD" in body)
custom_tpl = "Hey! {{subscription}} owes {{amount}} by {{due_date}}"
body2 = build_reminder_body(fake_rows, "2026-06-01", custom_tpl)
check("custom body template renders vars", "Acme owes 25.00 USD by 2026-07-01" in body2)
subj = _render_tpl("Bills due ({{count}}) for {{subscription}}", {"count": 3, "subscription": "Acme"})
check("subject template renders count + var", subj == "Bills due (3) for Acme")

# settings: change default currency + hide a sidebar tab
r = c.post("/settings", data={
    "default_currency": "EUR", "smtp_port": "587", "reminder_lead_days": "7",
    "show_dashboard": "1", "show_users": "1", "show_subscriptions": "1",
    "show_invoices": "1", "show_settings": "1",  # accounts intentionally omitted -> hidden
    "order_dashboard": "0", "order_users": "1", "order_subscriptions": "2",
    "order_invoices": "3", "order_accounts": "4", "order_settings": "5",
}, follow_redirects=True)
check("settings saved", "Settings saved" in r.text)
check("default currency now EUR", 'value="EUR" selected' in r.text or "EUR</option>" in r.text)
r = c.get("/")
check("hidden tab removed from sidebar", 'href="/accounts"' not in r.text)

# --- event notifications (catalog rules + CRUD + path matching) ---
from app.core import match_notifications  # noqa: E402
r = c.get("/notifications")
check("notifications page seeded", "Bill paid" in r.text and "/invoices/*/paid" in r.text)
# seeded rules are disabled by default -> nothing matches yet
check("seeded rules disabled by default", not match_notifications("/invoices/9/paid"))
# add a custom rule via GUI (enabled on add)
r = c.post("/notifications", data={"label": "Sub deleted",
           "match_path": "/subscriptions/*/delete", "recipient": ""}, follow_redirects=True)
check("add notification rule", "Sub deleted" in r.text)
check("added rule matches its path", any(x["label"] == "Sub deleted"
      for x in match_notifications("/subscriptions/3/delete")))
check("rule ignores non-matching path", not match_notifications("/users"))
with db.get_conn() as conn:
    paid_id = conn.execute("SELECT id FROM notifications WHERE label='Bill paid'").fetchone()[0]
    new_id = conn.execute("SELECT id FROM notifications WHERE label='Sub deleted'").fetchone()[0]
# toggle the seeded Bill-paid rule on
c.post(f"/notifications/{paid_id}/toggle")
check("toggle enables seeded rule", any(x["label"] == "Bill paid"
      for x in match_notifications("/invoices/9/paid")))
# update + delete
c.post(f"/notifications/{new_id}/update", data={"label": "Sub removed",
       "match_path": "/subscriptions/*/delete", "recipient": "ops@corp.com"})
r = c.get("/notifications")
check("update notification rule", "Sub removed" in r.text and "ops@corp.com" in r.text)
r = c.post(f"/notifications/{new_id}/delete", follow_redirects=True)
check("delete notification rule", "Sub removed" not in r.text)

# catch-all rule + delete coverage + per-action subject + recipient de-dupe (mocked SMTP)
import app.core as core_mod  # noqa: E402
db.set_setting("smtp_host", "smtp.test")
db.set_setting("reminder_to", "ops@corp.com")
db.add_notification("All activity", "/*", "")                  # enabled, uses reminder_to
db.add_notification("Dup users", "/users/*", "ops@corp.com")   # enabled, same recipient
check("catch-all matches a delete path", any(x["label"] == "All activity"
      for x in match_notifications("/users/5/delete")))
sent = []
core_mod._smtp_send = lambda s, msg: sent.append(msg)
core_mod.fire_notifications("/users/5/delete", "admin", 303, "User: delete",
                            "User deleted (and any seat assignments freed).")
check("catch-all fires email on delete", len(sent) >= 1)
check("subject names the action", any("User: delete" in m["Subject"] for m in sent))
check("body shows readable detail",
      any("User deleted (and any seat assignments freed)." in m.get_content() for m in sent))
check("body shows who performed it", any("admin" in m.get_content() for m in sent))
check("de-dupe: one email per recipient", [m["To"] for m in sent].count("ops@corp.com") == 1)
# undo test rules + SMTP so later POSTs in this suite don't fire notifications
db.set_setting("smtp_host", "")
with db.get_conn() as conn:
    conn.execute("UPDATE notifications SET enabled = 0")

# --- system log (audit middleware + view + export + clear) ---
r = c.get("/logs")
check("system log lists POST actions", "Create user" in r.text or "Create subscription" in r.text)
check("system log records the account", "tester" in r.text)
with db.get_conn() as conn:
    before = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
check("audit middleware logged something", before > 0)
# export logs
r = c.get("/export?kind=logs&fmt=csv")
check("export logs csv", r.status_code == 200 and b"action" in r.content)
# clear logs (the clear action itself is then logged by the middleware -> exactly 1 remains)
r = c.post("/logs/clear", follow_redirects=True)
check("clear logs ok", "cleared" in r.text.lower())
with db.get_conn() as conn:
    after = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
    act = conn.execute("SELECT action FROM logs").fetchone()
check("logs cleared, only the clear action remains", after == 1 and act[0] == "Clear system log")

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
