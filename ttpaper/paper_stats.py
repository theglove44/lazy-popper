#!/usr/bin/env python3
"""
Summarize paper trade results (requires settled rows).

Usage:
  python paper_stats.py --csv ./paper_trades.csv
"""

from __future__ import annotations

import argparse
import csv
import math
from typing import Dict, List, Tuple

def _parse_float(x: object, default: float = float("nan")) -> float:
    if x is None:
        return default
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip()
    if s == "" or s.lower() in {"nan", "none", "null"}:
        return default
    try:
        return float(s)
    except ValueError:
        return default

def read_rows(path: str) -> List[Dict[str, str]]:
    with open(path, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def is_settled(row: Dict[str, str]) -> bool:
    return (row.get("status") or "").strip().upper() == "SETTLED"

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="./paper_trades.csv")
    args = ap.parse_args()

    rows = read_rows(args.csv)
    settled = [r for r in rows if is_settled(r)]

    if not settled:
        print("No settled trades found. Run paper_settle.py after expiry.")
        return

    pnls = [_parse_float(r.get("pnl_usd_net")) for r in settled]
    pnls = [p for p in pnls if math.isfinite(p)]
    if not pnls:
        print("No numeric pnl_usd_net values found.")
        return

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    total = sum(pnls)
    win_rate = (len(wins) / len(pnls)) * 100.0

    avg = total / len(pnls)
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0

    # Expectancy per trade = avg pnl
    # Profit factor = sum(wins) / abs(sum(losses))
    pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")

    print(f"Trades settled: {len(pnls)}")
    print(f"Total P/L (net): ${total:,.2f}")
    print(f"Avg P/L per trade: ${avg:,.2f}")
    print(f"Win rate: {win_rate:.1f}%")
    print(f"Avg win: ${avg_win:,.2f} ({len(wins)} wins)")
    print(f"Avg loss: ${avg_loss:,.2f} ({len(losses)} losses)")
    print(f"Profit factor: {pf:.2f}")

if __name__ == "__main__":
    main()
