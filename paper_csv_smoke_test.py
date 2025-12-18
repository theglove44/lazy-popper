#!/usr/bin/env python3
from __future__ import annotations

import csv
import os
import uuid
from datetime import datetime, timezone

TEST_CSV = "./_paper_trades_test.csv"

# Keep this list aligned with your CLI row keys (minimum set is fine for this test)
REQUIRED_COLUMNS = [
    "paper_id",
    "created_at_utc",
    "cmd",
    "symbol",
    "exp",
    "qty",
    "short_strike",
    "long_strike",
    "short_delta",
    "limit_credit_points",
    "max_profit_usd",
    "max_loss_usd",
    "ivr",
    "bp_change",
    "fees_total",
]

def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()

def ensure_fresh_file(path: str) -> None:
    if os.path.exists(path):
        os.remove(path)

def append_row(path: str, row: dict) -> None:
    exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not exists:
            writer.writeheader()
        writer.writerow(row)

def read_rows(path: str) -> tuple[list[str], list[dict]]:
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)

def main() -> int:
    ensure_fresh_file(TEST_CSV)

    row1 = {
        "paper_id": str(uuid.uuid4()),
        "created_at_utc": now_utc(),
        "cmd": "buy",
        "symbol": "SPX",
        "exp": "2025-12-18",
        "qty": 1,
        "short_strike": 6720.0,
        "long_strike": 6715.0,
        "short_delta": -0.3013,
        "limit_credit_points": 1.23,
        "max_profit_usd": 123.0,
        "max_loss_usd": 377.0,
        "ivr": 0.17,
        "bp_change": -500.0,
        "fees_total": 2.40,
    }

    row2 = {**row1, "paper_id": str(uuid.uuid4()), "cmd": "sell"}

    append_row(TEST_CSV, row1)
    append_row(TEST_CSV, row2)

    header, rows = read_rows(TEST_CSV)

    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise SystemExit(f"FAIL: missing required columns in header: {missing}")

    if len(rows) != 2:
        raise SystemExit(f"FAIL: expected 2 rows, got {len(rows)}")

    print("OK: CSV logging works")
    print(f"  path: {TEST_CSV}")
    print(f"  header_cols: {len(header)}")
    print(f"  rows: {len(rows)}")
    print(f"  first_paper_id: {rows[0].get('paper_id')}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
