#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv

from tastytrade import Session, DXLinkStreamer
from tastytrade.dxfeed import Quote, Greeks
from tastytrade.instruments import get_option_chain, OptionType


def require_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v


def mid_from_quote(q: Quote) -> Optional[float]:
    bid = getattr(q, "bid_price", None)
    ask = getattr(q, "ask_price", None)
    last = getattr(q, "last_price", None)

    def f(x):
        try:
            return float(x)
        except Exception:
            return None

    b, a, l = f(bid), f(ask), f(last)
    if b is not None and a is not None:
        return (b + a) / 2.0
    if l is not None:
        return l
    if b is not None:
        return b
    if a is not None:
        return a
    return None


async def get_spot_mid(session: Session, symbol: str, timeout_s: float = 2.5) -> float:
    async with DXLinkStreamer(session) as streamer:
        await streamer.subscribe(Quote, [symbol])

        async def _wait_one():
            async for q in streamer.listen(Quote):
                if getattr(q, "event_symbol", None) == symbol:
                    m = mid_from_quote(q)
                    if m is not None:
                        return m

        try:
            m = await asyncio.wait_for(_wait_one(), timeout=timeout_s)
        except asyncio.TimeoutError:
            raise RuntimeError(f"Timed out waiting for Quote on {symbol} (DXLink)")
        if m is None:
            raise RuntimeError(f"No usable bid/ask/last for {symbol}")
        return float(m)


def pick_expiration(chain: dict, requested: Optional[str]) -> date:
    expirations = sorted(chain.keys())
    if not expirations:
        raise RuntimeError("Option chain returned no expirations")

    if requested:
        y, m, d = map(int, requested.split("-"))
        req = date(y, m, d)
        if req not in chain:
            raise RuntimeError(f"Requested exp {requested} not found in chain. Available: {expirations[:5]} ...")
        return req

    # Default: nearest expiration (today if available, otherwise next)
    return expirations[0]


@dataclass
class OptRow:
    streamer_symbol: str
    strike: float
    delta: float


async def scan_until_min_delta(
    session: Session,
    ordered_opts,                 # options ordered from near-OTM -> far-OTM
    min_abs_delta: float,
    batch_size: int,
    stop_n: int,
    timeout_s: float = 2.5,
):
    """
    Streams Greeks deltas in batches until the far-OTM tail reaches abs(delta) <= min_abs_delta
    for stop_n consecutive strikes.
    Returns: (deltas_map, used_streamer_symbols)
    """
    deltas: Dict[str, float] = {}
    used: List[str] = []

    async with DXLinkStreamer(session) as streamer:
        idx = 0
        tail_hits = 0

        while idx < len(ordered_opts):
            batch = ordered_opts[idx : idx + batch_size]
            idx += batch_size

            batch_syms = [o.streamer_symbol for o in batch]
            used.extend(batch_syms)
            await streamer.subscribe(Greeks, batch_syms)

            # collect deltas for this batch (or timeout)
            async def _gather_batch():
                remaining = set(batch_syms)
                async for g in streamer.listen(Greeks):
                    sym = getattr(g, "event_symbol", None)
                    if sym in remaining:
                        d = getattr(g, "delta", None)
                        if d is not None:
                            deltas[sym] = float(d)
                            remaining.remove(sym)
                    if not remaining:
                        return

            try:
                await asyncio.wait_for(_gather_batch(), timeout=timeout_s)
            except asyncio.TimeoutError:
                pass

            # update stopping condition using the most-OTM part we've seen so far
            # (ordered_opts is near->far OTM, so the latest batch is the farthest so far)
            # We check from the end of `used` backwards for consecutive abs(delta) <= threshold.
            tail_hits = 0
            for sym in reversed(used):
                d = deltas.get(sym)
                if d is None:
                    continue
                if abs(d) <= min_abs_delta:
                    tail_hits += 1
                    if tail_hits >= stop_n:
                        return deltas, used
                else:
                    break

        return deltas, used


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Test option chain + DXLink Greeks (delta) for SPX.")
    parser.add_argument("--symbol", default="SPX")
    parser.add_argument("--right", choices=["put", "call"], default="put")
    parser.add_argument("--exp", default=None, help="YYYY-MM-DD (optional). If omitted, uses nearest expiration.")
    parser.add_argument("--candidates", type=int, default=160, help="Maximum number of strikes to consider for scanning (sorted by proximity to spot).")
    parser.add_argument("--min-abs-delta", type=float, default=0.10, help="Stop scanning once we reach this abs(delta) OTM.")
    parser.add_argument("--batch-size", type=int, default=25, help="How many strikes to subscribe per batch.")
    parser.add_argument("--stop-n", type=int, default=3, help="How many consecutive OTM strikes must be <= min-abs-delta to stop.")
    parser.add_argument("--print", dest="print_n", type=int, default=30, help="How many strikes to print.")
    args = parser.parse_args()

    session = Session(require_env("TT_CLIENT_SECRET"), require_env("TT_REFRESH_TOKEN"))

    # 1) Chain
    chain = get_option_chain(session, args.symbol)
    exp = pick_expiration(chain, args.exp)
    options = list(chain[exp])
    if not options:
        raise RuntimeError(f"No options returned for {args.symbol} exp {exp}")

    # 2) Spot via DXLink quote
    spot = asyncio.run(get_spot_mid(session, args.symbol))

    # 3) Filter by option type
    want_type = OptionType.PUT if args.right == "put" else OptionType.CALL
    filtered = [o for o in options if o.option_type == want_type]
    
    # order near-OTM -> far-OTM (monotonic)
    if args.right == "put":
        # just below spot downwards
        filtered.sort(key=lambda o: float(o.strike_price), reverse=True)
        filtered = [o for o in filtered if float(o.strike_price) < spot]
        target_delta = -0.30
    else:
        # just above spot upwards
        filtered.sort(key=lambda o: float(o.strike_price))
        filtered = [o for o in filtered if float(o.strike_price) > spot]
        target_delta = +0.30

    if not filtered:
        raise RuntimeError("No candidate options after filtering")

    # 4) Greeks deltas via streaming scan
    deltas, used_syms = asyncio.run(
        scan_until_min_delta(
            session,
            filtered,
            min_abs_delta=args.min_abs_delta,
            batch_size=args.batch_size,
            stop_n=args.stop_n,
        )
    )

    # Build rows we actually received
    sym_to_strike = {o.streamer_symbol: float(o.strike_price) for o in filtered}
    rows = []
    for sym, d in deltas.items():
        rows.append(OptRow(streamer_symbol=sym, strike=sym_to_strike.get(sym, float("nan")), delta=d))
    rows.sort(key=lambda r: r.strike)

    print(f"\nOK: chain fetched for {args.symbol}")
    print(f"  expiration: {exp}")
    print(f"  spot_mid:   {spot:.2f}")
    print(f"  right:      {args.right}")
    print(f"  min_abs_delta: {args.min_abs_delta}")
    print(f"  stop_n:    {args.stop_n}\n")

    if not rows:
        print("No greeks received. Common causes: market closed, delayed data permissions, DXLink issues.", file=sys.stderr)
        return 2

    best = min(rows, key=lambda r: abs(r.delta - target_delta))
    print(f"Subscribed (greeks): {len(used_syms)}")
    print(f"Received deltas:     {len(rows)}")
    print(f"Closest to {target_delta:+.2f}: strike {best.strike:.2f}, delta {best.delta:+.4f}")
    
    # Print sample
    print("\nSample deltas:")
    for r in rows[: args.print_n]:
        print(f"  strike={r.strike:8.2f}  delta={r.delta:+.4f}  sym={r.streamer_symbol}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"FAIL: {e}", file=sys.stderr)
        raise SystemExit(1)
