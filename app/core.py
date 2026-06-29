"""Shared helpers: sidebar model, currencies, section summaries, email reminders."""
import fnmatch
import smtplib
from calendar import monthrange
from datetime import date, timedelta
from email.message import EmailMessage

from .db import (
    add_log, all_settings, enabled_notifications, get_conn, get_setting, today_iso,
)

# System tabs in their natural order. section_key -> label/url.
SYSTEM_SECTIONS = [
    {"key": "dashboard", "label": "Dashboard", "url": "/"},
    {"key": "users", "label": "Users", "url": "/users"},
    {"key": "subscriptions", "label": "Subscriptions", "url": "/subscriptions"},
    {"key": "invoices", "label": "Billing", "url": "/invoices"},
    {"key": "accounts", "label": "Accounts", "url": "/accounts"},
    {"key": "notifications", "label": "Notifications", "url": "/notifications"},
    {"key": "logs", "label": "System Log", "url": "/logs"},
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


def compute_charge(unit_cost, consumed, daily_basis=False, next_due=None, today=None):
    """Current charge for a subscription.

    Normal: unit_cost x seats consumed (per period).
    Daily basis: prorate from today to the next due date, using a daily rate of
    unit_cost / (days in the current month). Falls back to the full per-period
    charge when there is no upcoming due date.
    """
    base = (unit_cost or 0) * consumed
    if not daily_basis or not next_due:
        return base
    try:
        d_today = date.fromisoformat(today or today_iso())
        d_due = date.fromisoformat(next_due)
    except (TypeError, ValueError):
        return base
    days = (d_due - d_today).days
    if days <= 0:
        return base
    period = monthrange(d_today.year, d_today.month)[1]
    daily = (unit_cost or 0) / period
    return daily * days * consumed


def next_due_date(conn, subscription_id, today=None):
    """Nearest upcoming due date for a subscription's still-due invoices, or None."""
    row = conn.execute(
        "SELECT MIN(due_date) FROM invoices "
        "WHERE subscription_id = ? AND status = 'due' "
        "AND due_date IS NOT NULL AND due_date >= ?",
        (subscription_id, today or today_iso()),
    ).fetchone()
    return row[0] if row else None


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


DEFAULT_SUBJECT_TPL = "Subscription bills due ({{count}})"
DEFAULT_BODY_TPL = (
    "Subscription bills that need attention:\n"
    "{% for row in rows %}"
    "- {{subscription}}: {{label}} — {{amount}} due {{due_date}} {{status}}\n"
    "{% endfor %}"
    "\nMark them paid in the Subscription Manager once settled."
)

# Variables exposed per invoice row in templates.
_ROW_VAR_DOCS = [
    ("{{subscription}}", "Subscription name"),
    ("{{label}}", "Invoice label (e.g. June 2026)"),
    ("{{amount}}", "Formatted amount with currency"),
    ("{{due_date}}", "Due date or 'no date'"),
    ("{{status}}", "'due' or 'OVERDUE'"),
]
# Subject-only extras.
_SUBJECT_VAR_DOCS = [("{{count}}", "Number of due invoices")]

ROW_VAR_DOCS = _ROW_VAR_DOCS
SUBJECT_VAR_DOCS = _SUBJECT_VAR_DOCS + _ROW_VAR_DOCS


def _render_tpl(tpl: str, mapping: dict) -> str:
    """Replace {{key}} placeholders. Unknown keys left as-is (no KeyError)."""
    import re
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(mapping.get(m.group(1), m.group(0))), tpl)


def _row_vars(r, today) -> dict:
    overdue = r["due_date"] and r["due_date"] < today
    return {
        "subscription": r["sub"],
        "label": r["label"],
        "amount": fmt_money(r["amount"], r["currency"]),
        "due_date": r["due_date"] or "no date",
        "status": "OVERDUE" if overdue else "due",
    }


def build_reminder_body(rows, today, body_tpl: str | None = None) -> str:
    tpl = (body_tpl or DEFAULT_BODY_TPL).strip()
    # Split on {% for row in rows %} / {% endfor %} markers to repeat the row block.
    import re
    m = re.search(r"\{%\s*for row in rows\s*%\}(.*?)\{%\s*endfor\s*%\}", tpl, re.DOTALL)
    if m:
        prefix = tpl[:m.start()]
        row_block = m.group(1)
        suffix = tpl[m.end():]
        rendered_rows = "".join(_render_tpl(row_block, _row_vars(r, today)) for r in rows)
        return prefix + rendered_rows + suffix
    # No loop markers: render once with vars from first row (simple subject-style use).
    first = _row_vars(rows[0], today) if rows else {}
    return _render_tpl(tpl, first)


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

    subject_tpl = s.get("reminder_subject_tpl") or DEFAULT_SUBJECT_TPL
    body_tpl = s.get("reminder_body_tpl") or None
    first_vars = _row_vars(rows[0], today) if rows else {}
    subject = _render_tpl(subject_tpl, {"count": len(rows), **first_vars})

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = s.get("smtp_from") or s.get("smtp_user") or "noreply@localhost"
    msg["To"] = to
    msg.set_content(build_reminder_body(rows, today, body_tpl))

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


# --- event notifications ----------------------------------------------------
def match_notifications(path: str):
    """Enabled notification rules whose glob pattern matches a POST path."""
    return [r for r in enabled_notifications() if fnmatch.fnmatch(path, r["match_path"])]


def _smtp_send(s: dict, msg: EmailMessage) -> None:
    port = int(s.get("smtp_port") or "587")
    use_tls = (s.get("smtp_tls") or "1") == "1"
    user = s.get("smtp_user") or ""
    pw = s.get("smtp_password") or ""
    with smtplib.SMTP(s.get("smtp_host"), port, timeout=30) as server:
        if use_tls:
            server.starttls()
        if user:
            server.login(user, pw)
        server.send_message(msg)


def fire_notifications(path: str, account, status, action: str = "") -> None:
    """Send an email for each enabled rule matching this POST path.

    `action` is the friendly description of what happened (e.g. "User: delete");
    it names the email so a single catch-all rule still produces meaningful subjects.

    Runs in a daemon thread (SMTP can block / time out), so it must never raise.
    Skips silently when SMTP is unconfigured or no rule matches.
    """
    try:
        matched = match_notifications(path)
        if not matched:
            return
        s = all_settings()
        if not s.get("smtp_host"):
            return
        sender = s.get("smtp_from") or s.get("smtp_user") or "noreply@localhost"
        what = action or (matched[0]["label"] if matched else path)
        sent_to = set()  # de-dupe when several rules target the same recipient
        for r in matched:
            to = (r["recipient"] or "").strip() or s.get("reminder_to", "")
            if not to or to in sent_to:
                continue
            sent_to.add(to)
            msg = EmailMessage()
            msg["Subject"] = f"[Subscription Manager] {what}"
            msg["From"] = sender
            msg["To"] = to
            msg.set_content(
                f"Activity: {what}\n"
                f"Rule: {r['label']}\n"
                f"By: {account or 'unknown'}\n"
                f"Path: {path}\n"
                f"Status: {status}\n"
            )
            try:
                _smtp_send(s, msg)
                add_log(account, "Notification sent", f"{what} -> {to}", status)
            except Exception as e:  # noqa: BLE001
                add_log(account, "Notification failed", f"{what}: {e}", None)
    except Exception:  # noqa: BLE001 — audit/notify must never break a request
        pass
