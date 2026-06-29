"""FastAPI app: GUI + validation for users / subscriptions / seats, plus export."""
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import export
from .auth import authenticate, hash_password
from .db import get_conn, init_db, now_iso

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE.parent / "data"
templates = Jinja2Templates(directory=str(BASE / "templates"))


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
            SELECT s.id, s.name, s.seats, s.created_at,
                   (SELECT COUNT(*) FROM assignments a WHERE a.subscription_id = s.id) AS consumed
            FROM subscriptions s ORDER BY s.name
            """
        ).fetchall()
    return templates.TemplateResponse(request, "subscriptions.html", ctx(request, subs=rows))


@app.post("/subscriptions")
def create_subscription(
    request: Request,
    name: str = Form(...),
    seats: int = Form(...),
    _: int = Depends(login_required),
):
    name = name.strip()
    if not name:
        flash(request, "Subscription name is required.", "error")
        return RedirectResponse("/subscriptions", status_code=303)
    if seats < 0:
        flash(request, "Seats cannot be negative.", "error")
        return RedirectResponse("/subscriptions", status_code=303)
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM subscriptions WHERE name = ?", (name,)
        ).fetchone()
        if exists:
            flash(request, f"Subscription '{name}' already exists.", "error")
            return RedirectResponse("/subscriptions", status_code=303)
        conn.execute(
            "INSERT INTO subscriptions (name, seats, created_at) VALUES (?, ?, ?)",
            (name, seats, now_iso()),
        )
    flash(request, f"Subscription '{name}' created with {seats} seats.", "success")
    return RedirectResponse("/subscriptions", status_code=303)


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
    return templates.TemplateResponse(
        request,
        "subscription_detail.html",
        ctx(request, sub=sub, assigned=assigned, available=available,
            consumed=len(assigned), free=sub["seats"] - len(assigned)),
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


# --- export -----------------------------------------------------------------
@app.get("/export")
def export_data(
    request: Request,
    kind: str = "all",
    fmt: str = "xlsx",
    _: int = Depends(login_required),
):
    if kind not in ("users", "subscriptions", "assignments", "all"):
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
