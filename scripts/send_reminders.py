r"""Send a digest email of due/overdue subscription bills.

Run on a schedule (Windows Task Scheduler) to get automatic reminders.
Reads SMTP config from the app's Settings (stored in data/app.db).

Usage:
    .\.venv\Scripts\python.exe scripts\send_reminders.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import send_reminders  # noqa: E402
from app.db import init_db  # noqa: E402


def main() -> int:
    init_db()
    try:
        ok, msg = send_reminders()
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}")
        return 1
    print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
