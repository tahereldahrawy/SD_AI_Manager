"""Shared helpers: sidebar model, currencies, section summaries, email reminders."""
import smtplib
from datetime import date, timedelta
from email.message import EmailMessage

from .db import all_settings, get_conn, get_setting, today_iso

# System tabs in their natural order. section_key -> label/url.
SYSTEM_SECTIONS = [
    {"key": "dashboard", "label": "Dashboard", "url": "/"},
    {"key": "users", "label": "Users", "url": "/users"},
    {"key": "subscriptions", "label": "Subscriptions", "url": "/subscriptions"},
    {"key": "invoices", "label": "Billing", "url": "/invoices"},
    {"key": "accounts", "label": "Accounts", "url": "/accounts"},
    {"key": "settings", "label": "Settings", "url": "/settings"},
]
SECTION_BY_KEY = {s["key"]: s for s in SYSTEM_SECTIONS}

# Sections a custom tab is allowed to summarize (settings excluded — config, not data).
SUMMARIZABLE = ["users", "subscriptions", "invoices", "accounts"]

CURRENCIES = ["USD", "EUR", "GBP", "EGP", "SAR", "AED", "JPY", "CAD", "AUD"]


def default_currency() -> str:
    return get_setting("default_currency", "USD") or "USD"


def fmt_money(amount, currency=None) -> str:
    cur = currency or default_currency()
    return f"{amount:,.2f} {cur}"


# --- sidebar (system order/visibility + custom tabs) ------------------------
def _csv_list(key: str) -> list[str]:
    raw = get_setting(key, "")
    return [x for x in (p.strip() for p in raw.split(",")) if x]


def sidebar_entries(active_path: str = "") -> list[dict]:
    order = _csv_list("sidebar_order")
    hidden = set(_csv_list("sidebar_hidden"))

    ordered_keys = [k for k in order if k in SECTION_BY_KEY]
    for s in SYSTEM_SECTIONS:  # append any not covered by saved order
        if s["key"] not in ordered_keys:
            ordered_keys.append(s["key"])

    entries = []
    for k in ordered_keys:
        if k in hidden:
            continue
        s = SECTION_BY_KEY[k]
        entries.append({**s, "custom": False})

    with get_conn() as conn:
        tabs = conn.execute(
            "SELECT id, name FROM custom_tabs ORDER BY name"
        ).fetchall()
    for t in tabs:
        entries.append(
            {"key": f"custom:{t['id']}", "label": t["name"],
             "url": f"/tabs/{t['id']}", "custom": True}
        )

    for e in entries:
        e["active"] = active_path == e["url"] or (
            e["url"] != "/" and active_path.startswith(e["url"])
        )
    return entries


# --- counts / summaries -----------------------------------------------------
def due_invoice_count() -> int:
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM invoices WHERE status = 'due'"
        ).fetchone()[0]


def section_summary(key: str) -> dict:
    """Compact stat lines for a section, used by custom summary tabs."""
    s = SECTION_BY_KEY.get(key, {"label": key, "url": "#"})
    out = {"key": key, "label": s["label"], "url": s["url"], "lines": []}
    with get_conn() as conn:
        if key == "users":
            n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            out["lines"] = [f"{n} users"]
        elif key == "subscriptions":
            n = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
            seats = conn.execute(
                "SELECT COALESCE(SUM(seats),0) FROM subscriptions"
            ).fetchone()[0]
            used = conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0]
            out["lines"] = [f"{n} subscriptions", f"{used}/{seats} seats used"]
        elif key == "invoices":
            due = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount),0) FROM invoices WHERE status='due'"
            ).fetchone()
            paid = conn.execute(
                "SELECT COUNT(*) FROM invoices WHERE status='paid'"
            ).fetchone()[0]
            out["lines"] = [
                f"{due[0]} due ({fmt_money(due[1])})",
                f"{paid} paid",
            ]
        elif key == "accounts":
            n = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            out["lines"] = [f"{n} login accounts"]
    return out


# --- email reminders (used by app "send now" and the scheduled script) ------
def _due_for_reminder(lead_days: int):
    cutoff = (date.today() + timedelta(days=lead_days)).isoformat()
    today = today_iso()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT i.label, i.amount, i.currency, i.due_date, s.name AS sub
            FROM invoices i JOIN subscriptions s ON s.id = i.subscription_id
            WHERE i.status = 'due'
              AND (i.due_date IS NULL OR i.due_date <= ?)
            ORDER BY i.due_date IS NULL, i.due_date
            """,
            (cutoff,),
        ).fetchall()
    return rows, today


def build_reminder_body(rows, today) -> str:
    lines = ["Subscription bills that need attention:\n"]
    for r in rows:
        when = r["due_date"] or "no date"
        overdue = " (OVERDUE)" if r["due_date"] and r["due_date"] < today else ""
        lines.append(
            f"- {r['sub']}: {r['label']} — {fmt_money(r['amount'], r['currency'])} "
            f"due {when}{overdue}"
        )
    lines.append("\nMark them paid in the Subscription Manager once settled.")
    return "\n".join(lines)


def send_reminders() -> tuple[bool, str]:
    """Send one digest email of due/overdue invoices. Returns (ok, message)."""
    s = all_settings()
    host = s.get("smtp_host", "")
    to = s.get("reminder_to", "")
    if not host or not to:
        return False, "SMTP host and recipient must be set in Settings first."

    try:
        lead = int(s.get("reminder_lead_days") or "7")
    except ValueError:
        lead = 7

    rows, today = _due_for_reminder(lead)
    if not rows:
        return True, "No due invoices within the reminder window — nothing to send."

    msg = EmailMessage()
    msg["Subject"] = f"Subscription bills due ({len(rows)})"
    msg["From"] = s.get("smtp_from") or s.get("smtp_user") or "noreply@localhost"
    msg["To"] = to
    msg.set_content(build_reminder_body(rows, today))

    port = int(s.get("smtp_port") or "587")
    use_tls = (s.get("smtp_tls") or "1") == "1"
    user = s.get("smtp_user") or ""
    pw = s.get("smtp_password") or ""

    with smtplib.SMTP(host, port, timeout=30) as server:
        if use_tls:
            server.starttls()
        if user:
            server.login(user, pw)
        server.send_message(msg)
    return True, f"Reminder sent to {to} ({len(rows)} invoice(s))."
