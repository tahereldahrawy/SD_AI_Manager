"""Export users / subscriptions / assignments to CSV or XLSX.

CSV of "all" -> zip of three CSVs (CSV holds one table only).
XLSX of "all" -> one workbook, three sheets.
"""
import csv
import io
import zipfile

from openpyxl import Workbook

from .db import get_conn

# (header row, list-of-tuples) per dataset, computed on demand.
DATASETS = ("users", "subscriptions", "assignments")


def _fetch(kind: str):
    with get_conn() as conn:
        if kind == "users":
            rows = conn.execute(
                "SELECT id, name, email, created_at FROM users ORDER BY id"
            ).fetchall()
            headers = ["id", "name", "email", "created_at"]
            return headers, [tuple(r) for r in rows]

        if kind == "subscriptions":
            rows = conn.execute(
                """
                SELECT s.id, s.name, s.seats,
                       (SELECT COUNT(*) FROM assignments a WHERE a.subscription_id = s.id) AS consumed,
                       s.created_at
                FROM subscriptions s ORDER BY s.id
                """
            ).fetchall()
            headers = ["id", "name", "seats", "seats_consumed", "created_at"]
            return headers, [tuple(r) for r in rows]

        if kind == "assignments":
            rows = conn.execute(
                """
                SELECT a.subscription_id, s.name AS subscription, a.user_id,
                       u.name AS user, u.email, a.created_at
                FROM assignments a
                JOIN subscriptions s ON s.id = a.subscription_id
                JOIN users u ON u.id = a.user_id
                ORDER BY a.subscription_id, a.user_id
                """
            ).fetchall()
            headers = ["subscription_id", "subscription", "user_id", "user", "email", "created_at"]
            return headers, [tuple(r) for r in rows]

    raise ValueError(f"unknown dataset: {kind}")


def _csv_bytes(headers, rows) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")  # BOM -> Excel opens UTF-8 cleanly


def to_csv(kind: str) -> tuple[bytes, str, str]:
    """Return (content, media_type, filename)."""
    if kind == "all":
        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
            for ds in DATASETS:
                h, r = _fetch(ds)
                zf.writestr(f"{ds}.csv", _csv_bytes(h, r))
        return zbuf.getvalue(), "application/zip", "export_all.zip"

    h, r = _fetch(kind)
    return _csv_bytes(h, r), "text/csv", f"{kind}.csv"


def to_xlsx(kind: str) -> tuple[bytes, str, str]:
    wb = Workbook()
    wb.remove(wb.active)
    targets = DATASETS if kind == "all" else (kind,)
    for ds in targets:
        h, r = _fetch(ds)
        ws = wb.create_sheet(title=ds[:31])
        ws.append(h)
        for row in r:
            ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    fname = "export_all.xlsx" if kind == "all" else f"{kind}.xlsx"
    return (
        buf.getvalue(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        fname,
    )
