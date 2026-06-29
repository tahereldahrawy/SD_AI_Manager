"""FastAPI app: GUI + validation for users / subscriptions / seats, plus export."""
import asyncio
import secrets
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, core, export
from .auth import authenticate, hash_password
from .db import (
    add_log, add_notification, all_settings, clear_logs, delete_notification,
    get_conn, get_setting, init_db, list_notifications, now_iso,
    set_notification_enabled, set_setting, today_iso, update_notification,
)

# Human-readable labels for audited POST paths (longest match wins for {id} routes).
_ACTION_LABELS = {
    "/login": "Sign in",
    "/users": "Create user",
    "/subscriptions": "Create subscription",
    "/accounts": "Create account",
    "/invoices": "Create invoice",
    "/reminders/send": "Send reminder email",
    "/tabs": "Create custom tab",
    "/settings": "Update settings",
    "/logs/clear": "Clear system log",
}


def _describe(method: str, path: str) -> str:
    """Friendly action label for a POST path; falls back to a verb derived from the suffix."""
    if path in _ACTION_LABELS:
        return _ACTION_LABELS[path]
    # /subscriptions/3/pricing -> "Subscription: pricing", /invoices/5/paid -> "Invoice: paid"
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 3 and parts[1].isdigit():
        section = parts[0].rstrip("s").capitalize()
        return f"{section}: {parts[2]}"
    if len(parts) == 3 and parts[2] in ("delete", "update"):
        return f"{parts[0].rstrip('s').capitalize()}: {parts[2]}"
    return f"{method} {path}"

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE.parent / "data"
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.globals["fmt_money"] = core.fmt_money
templates.env.globals["CURRENCIES"] = core.CURRENCIES
templates.env.globals["app_version"] = __version__


def _secret_key() -> str:
    """Persist a session secret so cookies survive restarts."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    f = DATA_DIR / "secret.key"
    if not f.exists():
        f.write_text(secrets.token_hex(32))
    return f.read_text().strip()


app = FastAPI(title="Subscription Manager")
app.add_middleware(SessionMiddleware, secret_key=_secret_key(), https_only=True)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


@app.on_event("startup")
def _startup():
    init_db()


@app.on_event("startup")
async def _silence_proactor_reset():
    """Windows ProactorEventLoop logs a harmless ConnectionResetError (WinError 10054)
    when a client drops the connection abruptly (reload, tab close, cert warning).
    Swallow only that case; everything else logs as normal."""
    loop = asyncio.get_running_loop()
    default = loop.get_exception_handler()

    def handler(loop, context):
        if isinstance(context.get("exception"), ConnectionResetError):
            return
        (default or loop.default_exception_handler)(context)

    loop.set_exception_handler(handler)


@app.middleware("http")
async def _audit(request: Request, call_next):
    """Log every mutating request (POST) to the system log after it completes.
    Runs outside SessionMiddleware, so the session is populated by the time we read it."""
    response = await call_next(request)
    if request.method == "POST":
        try:
            user = request.session.get("username")
        except Exception:
            user = None
        path = request.url.path
        action = _describe(request.method, path)
        add_log(user, action, detail="", status=response.status_code)
        # Fire matching event notifications off the request path (SMTP may block).
        # Skip config churn (rule management, auth) so it never self-notifies.
        if response.status_code < 400 and not path.startswith(_NOTIFY_SKIP):
            threading.Thread(
                target=core.fire_notifications,
                args=(path, user, response.status_code, action),
                daemon=True,
            ).start()
    return response


# POST paths that should never trigger a notification (config/auth plumbing).
_NOTIFY_SKIP = ("/notifications", "/login", "/logout")


# --- auth plumbing ----------------------------------------------------------
class NotAuthenticated(Exception):
    pass


@app.exception_handler(NotAuthenticated)
async def _not_auth(request: Request, exc: NotAuthenticated):
    return RedirectResponse("/login", status_code=303)


def login_required(request: Request) -> int:
    aid = request.session.get("account_id")
    if not aid:
        raise NotAuthenticated()
    return aid


# --- flash messages (stored in session) -------------------------------------
def flash(request: Request, message: str, category: str = "info") -> None:
    request.session.setdefault("_flashes", []).append(
        {"category": category, "message": message}
    )


def ctx(request: Request, **kw):
    base = {"request": request, "flashes": request.session.pop("_flashes", [])}
    if request.session.get("account_id"):
        base["nav"] = core.sidebar_entries(request.url.path)
        base["due_count"] = core.due_invoice_count()
    base.update(kw)
    return base


# --- auth routes ------------------------------------------------------------
@app.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", ctx(request))


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    acct = authenticate(username, password)
    if not acct:
        flash(request, "Invalid username or password.", "error")
        return RedirectResponse("/login", status_code=303)
    request.session["account_id"] = acct["id"]
    request.session["username"] = acct["username"]
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --- dashboard --------------------------------------------------------------
@app.get("/")
def dashboard(request: Request, _: int = Depends(login_required)):
    with get_conn() as conn:
        n_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        n_subs = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
        n_assign = conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0]
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        ctx(request, n_users=n_users, n_subs=n_subs, n_assign=n_assign),
    )


# --- users ------------------------------------------------------------------
@app.get("/users")
def users_page(request: Request, _: int = Depends(login_required)):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT u.id, u.name, u.email, u.created_at,
                   (SELECT COUNT(*) FROM assignments a WHERE a.user_id = u.id) AS subs
            FROM users u ORDER BY u.name
            """
        ).fetchall()
    return templates.TemplateResponse(request, "users.html", ctx(request, users=rows))


@app.post("/users")
def create_user(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    _: int = Depends(login_required),
):
    name = name.strip()
    email = email.strip().lower()
    if not name or not email:
        flash(request, "Name and email are required.", "error")
        return RedirectResponse("/users", status_code=303)
    with get_conn() as conn:
        exists = conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone()
        if exists:
            flash(request, f"User with email '{email}' already exists.", "error")
            return RedirectResponse("/users", status_code=303)
        conn.execute(
            "INSERT INTO users (name, email, created_at) VALUES (?, ?, ?)",
            (name, email, now_iso()),
        )
    flash(request, f"User '{name}' created.", "success")
    return RedirectResponse("/users", status_code=303)


@app.post("/users/{user_id}/delete")
def delete_user(request: Request, user_id: int, _: int = Depends(login_required)):
    with get_conn() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    flash(request, "User deleted (and any seat assignments freed).", "success")
    return RedirectResponse("/users", status_code=303)


# --- subscriptions ----------------------------------------------------------
@app.get("/subscriptions")
def subs_page(request: Request, _: int = Depends(login_required)):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.name, s.seats, s.unit_cost, s.currency, s.daily_basis, s.created_at,
                   (SELECT COUNT(*) FROM assignments a WHERE a.subscription_id = s.id) AS consumed
            FROM subscriptions s ORDER BY s.name
            """
        ).fetchall()
    return templates.TemplateResponse(request, "subscriptions.html", ctx(request, subs=rows))


def _parse_cost(raw: str):
    """Optional unit cost. Returns (value_or_None, error_or_None)."""
    raw = (raw or "").strip()
    if not raw:
        return None, None
    try:
        v = float(raw)
    except ValueError:
        return None, "Unit cost must be a number."
    if v < 0:
        return None, "Unit cost cannot be negative."
    return v, None


def _clean_currency(raw: str):
    raw = (raw or "").strip().upper()
    return raw if raw in core.CURRENCIES else None


@app.post("/subscriptions")
def create_subscription(
    request: Request,
    name: str = Form(...),
    seats: int = Form(...),
    unit_cost: str = Form(""),
    currency: str = Form(""),
    daily_basis: str = Form(""),
    _: int = Depends(login_required),
):
    name = name.strip()
    cost, err = _parse_cost(unit_cost)
    if not name:
        flash(request, "Subscription name is required.", "error")
        return RedirectResponse("/subscriptions", status_code=303)
    if seats < 0:
        flash(request, "Seats cannot be negative.", "error")
        return RedirectResponse("/subscriptions", status_code=303)
    if err:
        flash(request, err, "error")
        return RedirectResponse("/subscriptions", status_code=303)
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM subscriptions WHERE name = ?", (name,)
        ).fetchone()
        if exists:
            flash(request, f"Subscription '{name}' already exists.", "error")
            return RedirectResponse("/subscriptions", status_code=303)
        conn.execute(
            "INSERT INTO subscriptions (name, seats, unit_cost, currency, daily_basis, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, seats, cost, _clean_currency(currency), 1 if daily_basis else 0, now_iso()),
        )
    flash(request, f"Subscription '{name}' created with {seats} seats.", "success")
    return RedirectResponse("/subscriptions", status_code=303)


@app.post("/subscriptions/{sub_id}/pricing")
def edit_pricing(
    request: Request,
    sub_id: int,
    unit_cost: str = Form(""),
    currency: str = Form(""),
    daily_basis: str = Form(""),
    _: int = Depends(login_required),
):
    cost, err = _parse_cost(unit_cost)
    if err:
        flash(request, err, "error")
    else:
        with get_conn() as conn:
            conn.execute(
                "UPDATE subscriptions SET unit_cost = ?, currency = ?, daily_basis = ? WHERE id = ?",
                (cost, _clean_currency(currency), 1 if daily_basis else 0, sub_id),
            )
        flash(request, "Pricing updated.", "success")
    return RedirectResponse(f"/subscriptions/{sub_id}", status_code=303)


@app.post("/subscriptions/{sub_id}/rename")
def rename_subscription(
    request: Request,
    sub_id: int,
    name: str = Form(...),
    _: int = Depends(login_required),
):
    name = name.strip()
    if not name:
        flash(request, "Name cannot be empty.", "error")
        return RedirectResponse(f"/subscriptions/{sub_id}", status_code=303)
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM subscriptions WHERE name = ? AND id != ?", (name, sub_id)
        ).fetchone()
        if exists:
            flash(request, f"Subscription '{name}' already exists.", "error")
            return RedirectResponse(f"/subscriptions/{sub_id}", status_code=303)
        conn.execute("UPDATE subscriptions SET name = ? WHERE id = ?", (name, sub_id))
    flash(request, "Subscription renamed.", "success")
    return RedirectResponse(f"/subscriptions/{sub_id}", status_code=303)


@app.post("/subscriptions/{sub_id}/delete")
def delete_subscription(request: Request, sub_id: int, _: int = Depends(login_required)):
    with get_conn() as conn:
        conn.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
    flash(request, "Subscription deleted.", "success")
    return RedirectResponse("/subscriptions", status_code=303)


@app.get("/subscriptions/{sub_id}")
def sub_detail(request: Request, sub_id: int, _: int = Depends(login_required)):
    with get_conn() as conn:
        sub = conn.execute(
            "SELECT * FROM subscriptions WHERE id = ?", (sub_id,)
        ).fetchone()
        if not sub:
            flash(request, "Subscription not found.", "error")
            return RedirectResponse("/subscriptions", status_code=303)
        assigned = conn.execute(
            """
            SELECT u.id, u.name, u.email FROM users u
            JOIN assignments a ON a.user_id = u.id
            WHERE a.subscription_id = ? ORDER BY u.name
            """,
            (sub_id,),
        ).fetchall()
        available = conn.execute(
            """
            SELECT u.id, u.name, u.email FROM users u
            WHERE u.id NOT IN (
                SELECT user_id FROM assignments WHERE subscription_id = ?
            ) ORDER BY u.name
            """,
            (sub_id,),
        ).fetchall()
        invoices = conn.execute(
            "SELECT * FROM invoices WHERE subscription_id = ? "
            "ORDER BY status, due_date IS NULL, due_date DESC",
            (sub_id,),
        ).fetchall()
    consumed = len(assigned)
    today = today_iso()
    daily = bool(sub["daily_basis"])
    with get_conn() as conn:
        nd = core.next_due_date(conn, sub_id, today)
    charge = core.compute_charge(sub["unit_cost"], consumed, daily, nd, today)
    return templates.TemplateResponse(
        request,
        "subscription_detail.html",
        ctx(request, sub=sub, assigned=assigned, available=available,
            consumed=consumed, free=sub["seats"] - consumed,
            invoices=invoices, charge=charge, daily_basis=daily, next_due=nd,
            currency=sub["currency"] or core.default_currency(),
            today=today),
    )


@app.post("/subscriptions/{sub_id}/seats")
def edit_seats(
    request: Request,
    sub_id: int,
    seats: int = Form(...),
    _: int = Depends(login_required),
):
    with get_conn() as conn:
        consumed = conn.execute(
            "SELECT COUNT(*) FROM assignments WHERE subscription_id = ?", (sub_id,)
        ).fetchone()[0]
        if seats < 0:
            flash(request, "Seats cannot be negative.", "error")
        elif seats < consumed:
            flash(
                request,
                f"Cannot set seats to {seats}: {consumed} already consumed. "
                "Unassign users first.",
                "error",
            )
        else:
            conn.execute(
                "UPDATE subscriptions SET seats = ? WHERE id = ?", (seats, sub_id)
            )
            flash(request, f"Seats updated to {seats}.", "success")
    return RedirectResponse(f"/subscriptions/{sub_id}", status_code=303)


@app.post("/subscriptions/{sub_id}/assign")
def assign_user(
    request: Request,
    sub_id: int,
    user_id: int = Form(...),
    _: int = Depends(login_required),
):
    with get_conn() as conn:
        sub = conn.execute(
            "SELECT seats FROM subscriptions WHERE id = ?", (sub_id,)
        ).fetchone()
        consumed = conn.execute(
            "SELECT COUNT(*) FROM assignments WHERE subscription_id = ?", (sub_id,)
        ).fetchone()[0]
        already = conn.execute(
            "SELECT 1 FROM assignments WHERE subscription_id = ? AND user_id = ?",
            (sub_id, user_id),
        ).fetchone()
        if already:
            flash(request, "User already assigned to this subscription.", "error")
        elif consumed >= sub["seats"]:
            flash(
                request,
                f"No free seats: {consumed}/{sub['seats']} consumed. "
                "Increase seats first.",
                "error",
            )
        else:
            conn.execute(
                "INSERT INTO assignments (user_id, subscription_id, created_at) "
                "VALUES (?, ?, ?)",
                (user_id, sub_id, now_iso()),
            )
            flash(request, "User assigned.", "success")
    return RedirectResponse(f"/subscriptions/{sub_id}", status_code=303)


@app.post("/subscriptions/{sub_id}/unassign")
def unassign_user(
    request: Request,
    sub_id: int,
    user_id: int = Form(...),
    _: int = Depends(login_required),
):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM assignments WHERE subscription_id = ? AND user_id = ?",
            (sub_id, user_id),
        )
    flash(request, "User unassigned (seat freed).", "success")
    return RedirectResponse(f"/subscriptions/{sub_id}", status_code=303)


# --- accounts (login users) -------------------------------------------------
MIN_PW = 6


@app.get("/accounts")
def accounts_page(request: Request, _: int = Depends(login_required)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, username, created_at FROM accounts ORDER BY username"
        ).fetchall()
    return templates.TemplateResponse(
        request,
        "accounts.html",
        ctx(request, accounts=rows, me=request.session.get("account_id")),
    )


@app.post("/accounts")
def create_account_route(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    _: int = Depends(login_required),
):
    username = username.strip()
    if not username:
        flash(request, "Username is required.", "error")
    elif len(password) < MIN_PW:
        flash(request, f"Password too short (min {MIN_PW} chars).", "error")
    else:
        with get_conn() as conn:
            exists = conn.execute(
                "SELECT 1 FROM accounts WHERE username = ?", (username,)
            ).fetchone()
            if exists:
                flash(request, f"Account '{username}' already exists.", "error")
            else:
                conn.execute(
                    "INSERT INTO accounts (username, password_hash, created_at) "
                    "VALUES (?, ?, ?)",
                    (username, hash_password(password), now_iso()),
                )
                flash(request, f"Account '{username}' created.", "success")
    return RedirectResponse("/accounts", status_code=303)


@app.post("/accounts/{account_id}/update")
def update_account(
    request: Request,
    account_id: int,
    username: str = Form(...),
    password: str = Form(""),
    _: int = Depends(login_required),
):
    username = username.strip()
    with get_conn() as conn:
        target = conn.execute(
            "SELECT 1 FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if not target:
            flash(request, "Account not found.", "error")
        elif not username:
            flash(request, "Username is required.", "error")
        elif conn.execute(
            "SELECT 1 FROM accounts WHERE username = ? AND id <> ?",
            (username, account_id),
        ).fetchone():
            flash(request, f"Username '{username}' is taken.", "error")
        elif password and len(password) < MIN_PW:
            flash(request, f"Password too short (min {MIN_PW} chars).", "error")
        else:
            conn.execute(
                "UPDATE accounts SET username = ? WHERE id = ?", (username, account_id)
            )
            if password:
                conn.execute(
                    "UPDATE accounts SET password_hash = ? WHERE id = ?",
                    (hash_password(password), account_id),
                )
            # keep nav label in sync if you renamed yourself
            if account_id == request.session.get("account_id"):
                request.session["username"] = username
            flash(request, "Account updated.", "success")
    return RedirectResponse("/accounts", status_code=303)


@app.post("/accounts/{account_id}/delete")
def delete_account(request: Request, account_id: int, _: int = Depends(login_required)):
    with get_conn() as conn:
        if account_id == request.session.get("account_id"):
            flash(request, "You cannot delete the account you are logged in as.", "error")
        elif conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] <= 1:
            flash(request, "Cannot delete the last account.", "error")
        else:
            conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            flash(request, "Account deleted.", "success")
    return RedirectResponse("/accounts", status_code=303)


# --- invoices / billing reminders -------------------------------------------
@app.get("/invoices")
def invoices_page(request: Request, status: str = "", _: int = Depends(login_required)):
    with get_conn() as conn:
        q = (
            "SELECT i.*, s.name AS sub_name FROM invoices i "
            "JOIN subscriptions s ON s.id = i.subscription_id"
        )
        params = ()
        if status in ("due", "paid"):
            q += " WHERE i.status = ?"
            params = (status,)
        q += " ORDER BY i.status, i.due_date IS NULL, i.due_date"
        invoices = conn.execute(q, params).fetchall()
        # subscriptions + their current per-period charge to prefill the form
        subs = conn.execute(
            """
            SELECT s.id, s.name, s.unit_cost, s.currency,
                   (SELECT COUNT(*) FROM assignments a WHERE a.subscription_id = s.id) AS consumed
            FROM subscriptions s ORDER BY s.name
            """
        ).fetchall()
    return templates.TemplateResponse(
        request, "invoices.html",
        ctx(request, invoices=invoices, subs=subs, status=status, today=today_iso(),
            default_currency=core.default_currency()),
    )


@app.post("/invoices")
def create_invoice(
    request: Request,
    subscription_id: int = Form(...),
    label: str = Form(...),
    due_date: str = Form(""),
    amount: str = Form(""),
    _: int = Depends(login_required),
):
    label = label.strip()
    if not label:
        flash(request, "Invoice label is required (e.g. 'June 2026').", "error")
        return RedirectResponse("/invoices", status_code=303)
    with get_conn() as conn:
        sub = conn.execute(
            "SELECT * FROM subscriptions WHERE id = ?", (subscription_id,)
        ).fetchone()
        if not sub:
            flash(request, "Subscription not found.", "error")
            return RedirectResponse("/invoices", status_code=303)
        if (amount or "").strip():
            amt, err = _parse_cost(amount)
            if err:
                flash(request, err.replace("Unit cost", "Amount"), "error")
                return RedirectResponse("/invoices", status_code=303)
        else:
            consumed = conn.execute(
                "SELECT COUNT(*) FROM assignments WHERE subscription_id = ?",
                (subscription_id,),
            ).fetchone()[0]
            amt = core.compute_charge(
                sub["unit_cost"], consumed, bool(sub["daily_basis"]),
                due_date.strip() or None, today_iso(),
            )
        conn.execute(
            "INSERT INTO invoices (subscription_id, label, amount, currency, "
            "due_date, status, created_at) VALUES (?, ?, ?, ?, ?, 'due', ?)",
            (subscription_id, label, amt, sub["currency"] or core.default_currency(),
             due_date.strip() or None, now_iso()),
        )
    flash(request, "Invoice created (marked due).", "success")
    return RedirectResponse(request.headers.get("referer", "/invoices"), status_code=303)


@app.post("/invoices/{invoice_id}/paid")
def mark_paid(request: Request, invoice_id: int, _: int = Depends(login_required)):
    with get_conn() as conn:
        conn.execute(
            "UPDATE invoices SET status = 'paid', paid_at = ? WHERE id = ?",
            (now_iso(), invoice_id),
        )
    flash(request, "Invoice marked paid.", "success")
    return RedirectResponse(request.headers.get("referer", "/invoices"), status_code=303)


@app.post("/invoices/{invoice_id}/due")
def mark_due(request: Request, invoice_id: int, _: int = Depends(login_required)):
    with get_conn() as conn:
        conn.execute(
            "UPDATE invoices SET status = 'due', paid_at = NULL WHERE id = ?",
            (invoice_id,),
        )
    flash(request, "Invoice reverted to due.", "info")
    return RedirectResponse(request.headers.get("referer", "/invoices"), status_code=303)


@app.post("/invoices/{invoice_id}/delete")
def delete_invoice(request: Request, invoice_id: int, _: int = Depends(login_required)):
    with get_conn() as conn:
        conn.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
    flash(request, "Invoice deleted.", "success")
    return RedirectResponse(request.headers.get("referer", "/invoices"), status_code=303)


@app.post("/reminders/send")
def send_reminders_now(request: Request, _: int = Depends(login_required)):
    try:
        ok, msg = core.send_reminders()
        flash(request, msg, "success" if ok else "error")
    except Exception as e:  # noqa: BLE001 - surface SMTP errors to the user
        flash(request, f"Email failed: {e}", "error")
    return RedirectResponse(request.headers.get("referer", "/invoices"), status_code=303)


# --- custom tabs ------------------------------------------------------------
@app.get("/tabs/new")
def new_tab_form(request: Request, _: int = Depends(login_required)):
    sections = [core.SECTION_BY_KEY[k] for k in core.SUMMARIZABLE]
    return templates.TemplateResponse(
        request, "custom_tab_new.html", ctx(request, sections=sections)
    )


@app.post("/tabs")
async def create_tab(request: Request, _: int = Depends(login_required)):
    form = await request.form()
    name = (form.get("name") or "").strip()
    chosen = [k for k in core.SUMMARIZABLE if form.get(f"section_{k}")]
    if not name:
        flash(request, "Tab name is required.", "error")
        return RedirectResponse("/tabs/new", status_code=303)
    if not chosen:
        flash(request, "Pick at least one section.", "error")
        return RedirectResponse("/tabs/new", status_code=303)
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO custom_tabs (name, created_at) VALUES (?, ?)",
            (name, now_iso()),
        )
        tab_id = cur.lastrowid
        for pos, k in enumerate(chosen):
            conn.execute(
                "INSERT INTO custom_tab_items (tab_id, section_key, position) "
                "VALUES (?, ?, ?)",
                (tab_id, k, pos),
            )
    flash(request, f"Custom tab '{name}' created.", "success")
    return RedirectResponse(f"/tabs/{tab_id}", status_code=303)


@app.get("/tabs/{tab_id}")
def view_tab(request: Request, tab_id: int, _: int = Depends(login_required)):
    with get_conn() as conn:
        tab = conn.execute(
            "SELECT * FROM custom_tabs WHERE id = ?", (tab_id,)
        ).fetchone()
        if not tab:
            flash(request, "Tab not found.", "error")
            return RedirectResponse("/", status_code=303)
        keys = [
            r["section_key"]
            for r in conn.execute(
                "SELECT section_key FROM custom_tab_items WHERE tab_id = ? "
                "ORDER BY position",
                (tab_id,),
            )
        ]
    cards = [core.section_summary(k) for k in keys]
    return templates.TemplateResponse(
        request, "custom_tab.html", ctx(request, tab=tab, cards=cards)
    )


@app.post("/tabs/{tab_id}/delete")
def delete_tab(request: Request, tab_id: int, _: int = Depends(login_required)):
    with get_conn() as conn:
        conn.execute("DELETE FROM custom_tabs WHERE id = ?", (tab_id,))
    flash(request, "Custom tab deleted.", "success")
    return RedirectResponse("/", status_code=303)


# --- settings ---------------------------------------------------------------
SETTING_KEYS = [
    "default_currency", "smtp_host", "smtp_port", "smtp_user", "smtp_password",
    "smtp_from", "smtp_tls", "reminder_to", "reminder_lead_days",
    "reminder_subject_tpl", "reminder_body_tpl",
]


@app.get("/settings")
def settings_page(request: Request, _: int = Depends(login_required)):
    s = all_settings()
    # sidebar customization model: each system section + show flag + order
    hidden = set(core._csv_list("sidebar_hidden"))
    order = core._csv_list("sidebar_order")
    sidebar = []
    for i, sec in enumerate(core.SYSTEM_SECTIONS):
        k = sec["key"]
        pos = order.index(k) if k in order else len(order) + i
        sidebar.append({"key": k, "label": sec["label"],
                        "show": k not in hidden, "order": pos})
    sidebar.sort(key=lambda x: x["order"])
    return templates.TemplateResponse(
        request, "settings.html",
        ctx(request, s=s, sidebar=sidebar, default_currency=core.default_currency(),
            default_subject_tpl=core.DEFAULT_SUBJECT_TPL,
            default_body_tpl=core.DEFAULT_BODY_TPL,
            row_var_docs=core.ROW_VAR_DOCS,
            subject_var_docs=core.SUBJECT_VAR_DOCS),
    )


@app.post("/settings")
async def settings_save(request: Request, _: int = Depends(login_required)):
    form = await request.form()
    for k in SETTING_KEYS:
        if k in form:
            set_setting(k, (form.get(k) or "").strip())
    set_setting("smtp_tls", "1" if form.get("smtp_tls") else "0")

    # sidebar: build order from numeric inputs, hidden from missing show flags
    pairs = []
    hidden = []
    for sec in core.SYSTEM_SECTIONS:
        k = sec["key"]
        try:
            pos = int(form.get(f"order_{k}", "0"))
        except ValueError:
            pos = 0
        pairs.append((pos, k))
        if not form.get(f"show_{k}"):
            hidden.append(k)
    pairs.sort()
    set_setting("sidebar_order", ",".join(k for _, k in pairs))
    set_setting("sidebar_hidden", ",".join(hidden))
    flash(request, "Settings saved.", "success")
    return RedirectResponse("/settings", status_code=303)


# --- event notifications ----------------------------------------------------
def _suggested_paths() -> list[str]:
    """POST endpoints as glob patterns ({id} -> *), for the add-rule datalist."""
    import re
    out = set()
    for r in app.routes:
        methods = getattr(r, "methods", None) or set()
        path = getattr(r, "path", "")
        if "POST" in methods and path and path != "/notifications":
            out.add(re.sub(r"\{[^}]+\}", "*", path))
    return sorted(out)


@app.get("/notifications")
def notifications_page(request: Request, _: int = Depends(login_required)):
    rules = list_notifications()
    smtp_ready = bool(get_setting("smtp_host", "") and get_setting("reminder_to", ""))
    return templates.TemplateResponse(
        request, "notifications.html",
        ctx(request, rules=rules, paths=_suggested_paths(), smtp_ready=smtp_ready),
    )


@app.post("/notifications")
def notification_add(
    request: Request,
    label: str = Form(...),
    match_path: str = Form(...),
    recipient: str = Form(""),
    _: int = Depends(login_required),
):
    label, match_path = label.strip(), match_path.strip()
    if not label or not match_path:
        flash(request, "Label and path pattern are both required.", "error")
    else:
        add_notification(label, match_path, recipient.strip())
        flash(request, f"Notification '{label}' added (enabled).", "success")
    return RedirectResponse("/notifications", status_code=303)


@app.post("/notifications/{nid}/update")
def notification_update(
    request: Request,
    nid: int,
    label: str = Form(...),
    match_path: str = Form(...),
    recipient: str = Form(""),
    _: int = Depends(login_required),
):
    label, match_path = label.strip(), match_path.strip()
    if not label or not match_path:
        flash(request, "Label and path pattern are both required.", "error")
    else:
        update_notification(nid, label, match_path, recipient.strip())
        flash(request, "Notification updated.", "success")
    return RedirectResponse("/notifications", status_code=303)


@app.post("/notifications/{nid}/toggle")
def notification_toggle(request: Request, nid: int, _: int = Depends(login_required)):
    with get_conn() as conn:
        row = conn.execute("SELECT enabled FROM notifications WHERE id = ?", (nid,)).fetchone()
    if row is not None:
        set_notification_enabled(nid, not row["enabled"])
        flash(request, "Notification " + ("disabled." if row["enabled"] else "enabled."), "success")
    return RedirectResponse("/notifications", status_code=303)


@app.post("/notifications/{nid}/delete")
def notification_delete(request: Request, nid: int, _: int = Depends(login_required)):
    delete_notification(nid)
    flash(request, "Notification deleted.", "success")
    return RedirectResponse("/notifications", status_code=303)


# --- system log -------------------------------------------------------------
@app.get("/logs")
def logs_page(request: Request, _: int = Depends(login_required)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM logs ORDER BY id DESC LIMIT 500"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
    return templates.TemplateResponse(
        request, "logs.html", ctx(request, logs=rows, total=total, shown=len(rows)),
    )


@app.post("/logs/clear")
def logs_clear(request: Request, _: int = Depends(login_required)):
    n = clear_logs()
    flash(request, f"System log cleared ({n} entries removed).", "success")
    return RedirectResponse("/logs", status_code=303)


# --- export -----------------------------------------------------------------
@app.get("/export")
def export_data(
    request: Request,
    kind: str = "all",
    fmt: str = "xlsx",
    _: int = Depends(login_required),
):
    if kind not in ("users", "subscriptions", "assignments", "invoices", "logs", "all"):
        flash(request, "Unknown export kind.", "error")
        return RedirectResponse("/", status_code=303)
    if fmt == "csv":
        content, media, fname = export.to_csv(kind)
    else:
        content, media, fname = export.to_xlsx(kind)
    return Response(
        content,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
