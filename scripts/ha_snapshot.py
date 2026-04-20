#!/usr/bin/env python3
"""Generate a consistent snapshot of memory_store.db using conn.backup().

Used during sync: scp this to the remote node, execute, then rsync the snapshot back.
conn.backup() is concurrency-safe — does not require a write lock, safe while Hermes is running.

Usage:
    python3 ha_snapshot.py --hermes-dir ~/.hermes
"""

import argparse
import sqlite3
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Snapshot memory_store.db")
    parser.add_argument("--hermes-dir", required=True, help="Hermes home directory")
    args = parser.parse_args()

    hermes = Path(args.hermes_dir)
    src = hermes / "memory_store.db"
    dst = hermes / "memory_store.snapshot.db"

    if not src.exists():
        print("No memory_store.db found", file=sys.stderr)
        sys.exit(1)

    dst.unlink(missing_ok=True)

    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(dst))
    src_conn.backup(dst_conn)
    dst_conn.close()
    src_conn.close()

    print(f"Snapshot: {dst.stat().st_size} bytes")


if __name__ == "__main__":
    main()
