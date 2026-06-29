# Subscription Manager

Small single-machine app to manage users, subscriptions, and seat assignments.
FastAPI + SQLite + server-rendered HTML. Runs over HTTPS on a LAN.

## What it does

- **Users** — create with unique email (duplicate emails rejected).
- **Subscriptions** — create with a unique name, editable seat count, and an
  **optional per-seat cost + currency**.
- **Assignments** — assign users to a subscription; each assignment consumes one seat.
  - Cannot assign past the seat limit.
  - Cannot shrink seats below the number already consumed.
  - Current charge = `unit_cost × seats consumed`, shown live on the subscription.
  - **Daily basis** (optional tick): prorate the charge from today to the next due
    date — `unit_cost / days-in-month × days-remaining × seats`. e.g. a $25 sub with
    10 days left in the cycle charges ≈ $8.33. Falls back to the full charge when no
    upcoming due date exists.
- **Billing & reminders** — create invoices per subscription (amount pre-fills from
  the current charge). Each is **due** until ticked **paid**. Overdue dates are
  flagged; the sidebar shows a due-count badge. Optional **email reminders** via SMTP.
- **Export** — Users, Subscriptions, Assignments, Invoices, or Everything, as CSV or Excel.
- **Login** — a small set of named accounts; full GUI account management; HTTPS-only sessions.
- **Left sidebar nav** with **custom tabs**: the "+" button builds a named tab that
  shows compact summary cards for the sections you pick.
- **Settings** page: default currency, SMTP config, reminder recipients/lead-days,
  and sidebar show/hide + ordering.

## Layout

```
app/
  main.py        routes + validation
  db.py          SQLite schema + migrations + settings
  core.py        sidebar model, currencies, summaries, email reminders
  auth.py        password hashing (pbkdf2_sha256)
  export.py      CSV / XLSX export
  templates/     HTML (Jinja2)
  static/        CSS
scripts/
  gen_cert.py            self-signed TLS cert
  create_account.py      add / reset a login account
  send_reminders.py      email a digest of due bills (run on a schedule)
  register_reminder_task.ps1  register the daily Windows scheduled task
run.ps1          one-command start (venv + deps + cert + server)
data/            SQLite DB, cert, session key (created at runtime, gitignored)
```

## First run (Windows / PowerShell)

```powershell
# 1. create the first login account (installs deps into the venv first time)
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts\create_account.py admin

# 2. start the server (generates the cert on first run)
.\run.ps1            # serves https://<this-machine>:8443
```

After the first account exists, manage the rest from the GUI: the **Accounts**
page (add / rename / change password / delete). The CLI script stays as a
bootstrap and password-reset fallback:

```powershell
.\.venv\Scripts\python.exe scripts\create_account.py alice
```

Account guards: usernames are unique, passwords min 6 chars, you cannot delete
the account you are logged in as, and the last remaining account cannot be deleted.

## Accessing from other LAN machines

Open `https://<server-hostname-or-ip>:8443`.

The certificate is self-signed, so browsers show a one-time warning — expected on a
LAN with no public domain. To avoid the warning, install `data\cert.pem` into the
client's **Trusted Root Certification Authorities** store. To include extra
hostnames/IPs in the cert, delete `data\cert.pem` and regenerate:

```powershell
.\.venv\Scripts\python.exe scripts\gen_cert.py myserver 192.168.1.50
```

## Email reminders (optional)

1. In the app, open **Settings** and fill SMTP host/port/user/password, the From
   address, recipient(s), and how many days before the due date to remind.
2. Test it with the **"Send reminder email now"** button on the Billing page.
3. To send automatically every day, register a Windows scheduled task:

```powershell
.\scripts\register_reminder_task.ps1 08:00   # daily at 08:00 (time optional)
```

The task runs `scripts\send_reminders.py`, which emails one digest of all due /
overdue bills within the reminder window. No email is sent if nothing is due.

## Security notes

- App binds with TLS directly (uvicorn `--ssl-*`); traffic is encrypted on the wire.
- Passwords are hashed (pbkdf2_sha256), never stored in plain text.
- Session cookies are `https_only`.
- Keep `data/` private — it holds the database, the TLS private key, and the
  session secret. Back up by copying `data\app.db`.
- The SMTP password is stored in the local `data\app.db` (gitignored). Acceptable
  for an internal LAN box; do not commit `data/` or expose it.
- Restrict access with the Windows Firewall to your LAN subnet if needed.
