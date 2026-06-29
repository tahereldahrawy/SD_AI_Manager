# Subscription Manager

Small single-machine app to manage users, subscriptions, and seat assignments.
FastAPI + SQLite + server-rendered HTML. Runs over HTTPS on a LAN.

## What it does

- **Users** — create with unique email (duplicate emails rejected).
- **Subscriptions** — create with a unique name and an editable seat count.
- **Assignments** — assign users to a subscription; each assignment consumes one seat.
  - Cannot assign past the seat limit.
  - Cannot shrink seats below the number already consumed.
- **Export** — Users, Subscriptions, Assignments, or Everything, as CSV or Excel.
- **Login** — a small set of named accounts; sessions over HTTPS only.

## Layout

```
app/
  main.py        routes + validation
  db.py          SQLite schema + connection
  auth.py        password hashing (pbkdf2_sha256)
  export.py      CSV / XLSX export
  templates/     HTML (Jinja2)
  static/        CSS
scripts/
  gen_cert.py    self-signed TLS cert
  create_account.py  add / reset a login account
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

## Security notes

- App binds with TLS directly (uvicorn `--ssl-*`); traffic is encrypted on the wire.
- Passwords are hashed (pbkdf2_sha256), never stored in plain text.
- Session cookies are `https_only`.
- Keep `data/` private — it holds the database, the TLS private key, and the
  session secret. Back up by copying `data\app.db`.
- Restrict access with the Windows Firewall to your LAN subnet if needed.
