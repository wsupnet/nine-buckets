"""Wipe user data before going live.

    docker compose run --rm networth python -m app.reset --yes
    docker compose run --rm networth python -m app.reset --yes --buckets-too

Keeps the nine buckets and their targets by default; --buckets-too clears those as
well so you can define your own from scratch. Takes a backup first either way.
"""

import argparse
import sys

from .db import connect, init_db
from .jobs import backup_database

TABLES = ["lots", "snapshot_lines", "snapshots", "items", "accounts"]


def counts(conn):
    out = {}
    for t in TABLES + ["buckets"]:
        try:
            out[t] = conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
        except Exception:
            out[t] = 0
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Clear user data from the tracker.")
    ap.add_argument("--yes", action="store_true", help="required; confirms the wipe")
    ap.add_argument("--buckets-too", action="store_true",
                    help="also delete the buckets and their targets")
    ap.add_argument("--no-backup", action="store_true", help="skip the safety backup")
    args = ap.parse_args(argv)

    init_db()
    conn = connect()

    before = counts(conn)
    print("Current contents:")
    for k, v in before.items():
        print(f"  {k:<15} {v}")

    if not args.yes:
        print("\nNothing changed. Re-run with --yes to actually clear the data.")
        conn.close()
        return 1

    if not args.no_backup:
        try:
            info = backup_database()
            print(f"\nSafety backup written: {info['file']} ({info['bytes']:,} bytes)")
        except Exception as exc:
            print(f"\nBackup failed ({exc}). Aborting — re-run with --no-backup to force.")
            conn.close()
            return 2

    for t in TABLES:
        conn.execute(f"DELETE FROM {t}")
    if args.buckets_too:
        conn.execute("DELETE FROM buckets")
    # Reset autoincrement counters so a fresh start has ids from 1.
    conn.execute("DELETE FROM sqlite_sequence") if conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
    ).fetchone() else None
    conn.commit()
    conn.execute("VACUUM")

    after = counts(conn)
    print("\nAfter reset:")
    for k, v in after.items():
        print(f"  {k:<15} {v}")
    conn.close()
    print("\nDone. Restart the app and start entering your own data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
