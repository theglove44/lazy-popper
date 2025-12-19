#!/usr/bin/env python3
"""
Settle SPXW paper trades to expiration and write results back into paper_trades.csv.

Assumptions:
- Trades are 5-wide (or any width) SPXW vertical *credit* spreads.
- SPXW is PM-settled based on the closing level of the S&P 500 index on expiration day.
- We approximate settlement using SPX daily close from Yahoo Finance (^GSPC) unless you override with --settlement.

Usage:
  python paper_settle.py --csv ./paper_trades.csv
  python paper_settle.py --csv ./paper_trades.csv --exp 2025-12-18 --settlement 6731.17
  python paper_settle.py --csv ./paper_trades.csv --source yahoo

Outputs:
- Updates/creates columns: status, settled_at_utc, settlement_spx, payoff_points,
  pnl_usd_gross, pnl_usd_net, ror_net, outcome
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import math
import os
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

try:
    import requests
except Exception as e:  # pragma: no cover
    requests = None  # type: ignore


MULTIPLIER = 100.0  # SPX/SPXW/XSP options multiplier is $100 per index point.


SETTLE_FIELDS = [
    "status",            # OPEN | SETTLED
    "settled_at_utc",    # ISO timestamp
    "settlement_spx",    # float
    "payoff_points",     # float (0..width)
    "pnl_usd_gross",     # float
    "pnl_usd_net",       # float (incl fees_total)
    "ror_net",           # float (net_pnl / risk_basis)
    "outcome",           # WIN | LOSS | BREAKEVEN
]


def _iso_utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


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


def _parse_int(x: object, default: int = 0) -> int:
    if x is None:
        return default
    if isinstance(x, int):
        return x
    s = str(x).strip()
    if s == "" or s.lower() in {"nan", "none", "null"}:
        return default
    try:
        return int(float(s))
    except ValueError:
        return default


def _ymd(date_iso: str) -> str:
    # YYYY-MM-DD -> YYYYMMDD
    return date_iso.replace("-", "")


def fetch_spx_close_yahoo(date_iso: str) -> float:
    """
    Fetch SPX daily close for a single trading date using Yahoo Finance chart API (^GSPC).

    Endpoint:
      https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?interval=1d&period1=...&period2=...

    We request a small window around date_iso and select the candle whose *America/New_York* date
    matches date_iso.

    Note: if date_iso is a market holiday/weekend, Yahoo may not return a candle for that date.
    """
    if requests is None:
        raise RuntimeError("requests is not installed; add it to requirements.txt")

    try:
        from zoneinfo import ZoneInfo  # py3.9+
        ny = ZoneInfo("America/New_York")
        def to_ny_date(ts: int) -> dt.date:
            return dt.datetime.fromtimestamp(int(ts), tz=dt.timezone.utc).astimezone(ny).date()
    except Exception:  # pragma: no cover
        def to_ny_date(ts: int) -> dt.date:  # type: ignore
            return dt.datetime.fromtimestamp(int(ts), tz=dt.timezone.utc).date()

    try:
        d = dt.date.fromisoformat(date_iso)
    except ValueError as e:
        raise RuntimeError(f"Bad date_iso {date_iso!r}") from e

    # Pull a 3-day window to robustly include the trading day.
    start = dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc) - dt.timedelta(days=1)
    end = dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc) + dt.timedelta(days=2)
    period1 = int(start.timestamp())
    period2 = int(end.timestamp())

    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC"
        f"?interval=1d&period1={period1}&period2={period2}&events=history"
    )
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, timeout=20, headers=headers)
    r.raise_for_status()
    data = r.json()

    chart = (data or {}).get("chart") or {}
    error = chart.get("error")
    if error:
        raise RuntimeError(f"Yahoo chart error for {date_iso}: {error}")

    results = chart.get("result") or []
    if not results:
        raise RuntimeError(f"No Yahoo chart result for {date_iso}")

    res0 = results[0]
    ts_list = res0.get("timestamp") or []
    indicators = (res0.get("indicators") or {}).get("quote") or []
    if not indicators:
        raise RuntimeError("Yahoo response missing indicators.quote")
    closes = indicators[0].get("close") or []

    if not ts_list or not closes or len(ts_list) != len(closes):
        raise RuntimeError("Yahoo response missing timestamps/closes")

    for ts, c in zip(ts_list, closes):
        if c is None:
            continue
        if to_ny_date(ts) == d:
            close = _parse_float(c)
            if math.isfinite(close):
                return close

    raise RuntimeError(f"No daily close found on Yahoo for {date_iso} (holiday/weekend or data unavailable)")

def payoff_points_vertical(settlement_spx: float, k1: float, k2: float, right: str) -> float:
    """
    Payoff (in index points) of the *long* vertical spread with strikes (k_low, k_high).

    put:  long put spread (k_high put - k_low put)
    call: long call spread (k_low call - k_high call)

    This payoff is always in [0, width] when using ordered strikes.
    """
    k_low = min(k1, k2)
    k_high = max(k1, k2)
    width = k_high - k_low

    right = right.lower().strip()
    if right == "put":
        payoff = max(k_high - settlement_spx, 0.0) - max(k_low - settlement_spx, 0.0)
    elif right == "call":
        payoff = max(settlement_spx - k_low, 0.0) - max(settlement_spx - k_high, 0.0)
    else:
        raise ValueError(f"Unsupported right={right!r}")

    # Numerical safety
    if payoff < 0:
        payoff = 0.0
    if payoff > width:
        payoff = width
    return payoff


def compute_pnl_usd(
    credit_points: float,
    payoff_points: float,
    qty: int,
    fees_total: float,
) -> Tuple[float, float]:
    """
    P/L for a short credit spread held to expiration.

    Gross P/L ignores fees. Net P/L includes fees_total (which should be negative).
    """
    pnl_points = credit_points - payoff_points
    pnl_usd_gross = pnl_points * MULTIPLIER * qty
    pnl_usd_net = pnl_usd_gross + fees_total
    return pnl_usd_gross, pnl_usd_net


def compute_ror_net(net_pnl: float, row: Dict[str, str], credit_points: float) -> float:
    """
    Return-on-risk basis.
    Preference order:
      1) abs(bp_change) if present and finite (dry-run includes fees in many cases)
      2) (width - credit)*100*qty - fees_total
    """
    bp_change = _parse_float(row.get("bp_change"))
    if math.isfinite(bp_change) and bp_change != 0.0:
        return net_pnl / abs(bp_change)

    width_points = _parse_float(row.get("width_points"))
    qty = _parse_int(row.get("qty"), 1)
    fees_total = _parse_float(row.get("fees_total"), 0.0)

    if not (math.isfinite(width_points) and math.isfinite(credit_points)):
        return float("nan")

    gross_max_loss = max(width_points - credit_points, 0.0) * MULTIPLIER * qty
    net_max_loss = gross_max_loss - fees_total  # fees_total is negative => increases loss
    if net_max_loss == 0.0:
        return float("nan")
    return net_pnl / net_max_loss


def outcome_label(net_pnl: float, eps: float = 1e-6) -> str:
    if net_pnl > eps:
        return "WIN"
    if net_pnl < -eps:
        return "LOSS"
    return "BREAKEVEN"


def should_settle(row: Dict[str, str], today_utc: dt.date, exp_filter: Optional[str], include_today: bool) -> bool:
    status = (row.get("status") or "OPEN").strip().upper()
    if status == "SETTLED":
        return False
    exp = (row.get("exp") or "").strip()
    if not exp:
        return False
    if exp_filter and exp != exp_filter:
        return False
    try:
        exp_date = dt.date.fromisoformat(exp)
    except ValueError:
        return False
    # Settle only expirations strictly before today (UTC). Run this the next day after expiry.
    return exp_date <= today_utc if include_today else exp_date < today_utc


def settle_file(
    csv_path: str,
    source: str,
    exp_filter: Optional[str],
    settlement_override: Optional[float],
    include_today: bool,
) -> int:
    today_utc = dt.datetime.now(dt.timezone.utc).date()

    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise RuntimeError("CSV has no header")
        fieldnames = list(reader.fieldnames)

        # Ensure settlement fields exist (append to end if missing)
        for c in SETTLE_FIELDS:
            if c not in fieldnames:
                fieldnames.append(c)

        rows: List[Dict[str, str]] = []
        settled_n = 0

        for row in reader:
            if should_settle(row, today_utc, exp_filter, include_today):
                exp = row["exp"].strip()

                # Choose settlement value
                if settlement_override is not None:
                    settlement_spx = float(settlement_override)
                else:
                    if source == "yahoo":
                        settlement_spx = fetch_spx_close_yahoo(exp)
                    else:
                        raise ValueError(f"Unknown --source {source!r}")

                right = (row.get("right") or "").strip().lower()
                k1 = _parse_float(row.get("short_strike"))
                k2 = _parse_float(row.get("long_strike"))
                qty = _parse_int(row.get("qty"), 1)
                fees_total = _parse_float(row.get("fees_total"), 0.0)

                # Prefer actual limit_credit_points if present; else fall back to mid
                credit_points = _parse_float(row.get("limit_credit_points"))
                if not math.isfinite(credit_points):
                    credit_points = _parse_float(row.get("credit_mid_points"))
                if not math.isfinite(credit_points):
                    raise RuntimeError(f"Missing credit points in row paper_id={row.get('paper_id')}")

                payoff_pts = payoff_points_vertical(settlement_spx, k1, k2, right)
                pnl_gross, pnl_net = compute_pnl_usd(credit_points, payoff_pts, qty, fees_total)
                ror = compute_ror_net(pnl_net, row, credit_points)

                row["status"] = "SETTLED"
                row["settled_at_utc"] = _iso_utc_now()
                row["settlement_spx"] = f"{settlement_spx:.4f}"
                row["payoff_points"] = f"{payoff_pts:.4f}"
                row["pnl_usd_gross"] = f"{pnl_gross:.2f}"
                row["pnl_usd_net"] = f"{pnl_net:.2f}"
                row["ror_net"] = "" if not math.isfinite(ror) else f"{ror:.6f}"
                row["outcome"] = outcome_label(pnl_net)

                settled_n += 1

            # Ensure all fields exist for writer
            for c in fieldnames:
                row.setdefault(c, "")
            rows.append(row)

    # Atomic rewrite
    tmp_path = csv_path + ".tmp"
    with open(tmp_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_path, csv_path)
    return settled_n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="./paper_trades.csv", help="Path to paper_trades.csv")
    ap.add_argument("--source", default="yahoo", choices=["yahoo"], help="Settlement source")
    ap.add_argument("--exp", default=None, help="Settle only this expiration YYYY-MM-DD")
    ap.add_argument("--settlement", default=None, type=float, help="Override settlement SPX level (points)")
    ap.add_argument("--include-today", action="store_true", help="Allow settling trades with exp == today (run after the close)")
    args = ap.parse_args()

    settled_n = settle_file(
        csv_path=args.csv,
        source=args.source,
        exp_filter=args.exp,
        settlement_override=args.settlement,
        include_today=args.include_today,
    )
    print(f"Settled {settled_n} trade(s) in {args.csv}")


if __name__ == "__main__":
    main()
