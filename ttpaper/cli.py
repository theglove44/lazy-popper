from __future__ import annotations

import asyncio
import csv
import json
import os
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()  # loads .env from current working directory (and parents)

from dataclasses import dataclass
from datetime import datetime, timezone, date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

import typer
from rich import print as rprint

from tastytrade import Account, DXLinkStreamer, Session
from tastytrade.dxfeed import Greeks
from tastytrade.instruments import OptionType, get_option_chain
from tastytrade.market_data import get_market_data, get_market_data_by_type
from tastytrade.metrics import get_market_metrics
from tastytrade.order import (
    InstrumentType,
    NewOrder,
    OrderAction,
    OrderTimeInForce,
    OrderType,
)
from ttpaper.paper_settle import settle_file

# --- Defaults / "presets" ---
DEFAULT_SYMBOL = "SPX"
DEFAULT_ABS_DELTA = 0.30
DEFAULT_MIN_ABS_DELTA = 0.10
DEFAULT_BATCH_SIZE = 25
DEFAULT_STOP_N = 3
DEFAULT_MAX_SCAN = 250  # hard safety cap (won't be hit usually)
DEFAULT_WIDTH_POINTS = 5.0
DEFAULT_QTY = 1

# Index contract multiplier $100 per point
INDEX_MULTIPLIER = Decimal("100")

# SPX min ticks: 0.05 < 3.00, else 0.10 :contentReference[oaicite:2]{index=2}
TICK_SMALL = Decimal("0.05")
TICK_LARGE = Decimal("0.10")
TICK_SWITCH = Decimal("3.00")

CSV_DEFAULT = os.environ.get("TT_PAPER_CSV", "./paper_trades.csv")

app = typer.Typer(add_completion=False)


@dataclass(frozen=True)
class LegPick:
    short_opt: object  # tastytrade.instruments.Option
    long_opt: object
    short_delta: float
    exp: object  # datetime.date
    spot_mid: float


def _env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _round_to_tick(x: Decimal) -> Decimal:
    tick = TICK_SMALL if x < TICK_SWITCH else TICK_LARGE
    return (x / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick


def _as_float(x) -> float:
    if x is None:
        return float("nan")
    try:
        return float(x)
    except Exception:
        return float("nan")


def _spot_mid(session: Session, symbol: str) -> float:
    # One-shot market data fetch (includes indices=["SPX", ...]) :contentReference[oaicite:3]{index=3}
    md = get_market_data(session, symbol, InstrumentType.INDEX)
    # Prefer mid, else mark, else last
    return _as_float(getattr(md, "mid", None) or getattr(md, "mark", None) or getattr(md, "last", None))


def _pick_expiration(chain: dict, *, prefer_0dte: bool = True):
    # Choose nearest expiration >= today (NY time is handled internally by SDK utils; chain keys are dates)
    # For SPX this will typically be 0DTE if there’s an expiry today.
    expirations = sorted(chain.keys())
    if not expirations:
        raise RuntimeError("No expirations returned by option chain")

    from tastytrade.utils import today_in_new_york
    today = today_in_new_york()

    future_or_today = [d for d in expirations if d >= today]
    if not future_or_today:
        return expirations[-1]
    return future_or_today[0] if prefer_0dte else min(future_or_today, key=lambda d: abs((d - today).days - 45))


async def _pick_short_by_delta(session: Session, options, target_delta: float, max_candidates: int) -> tuple[object, float]:
    # Subscribe to greeks for a limited set of nearby strikes using DXLink streamer :contentReference[oaicite:4]{index=4}
    symbols = [o.streamer_symbol for o in options[:max_candidates]]
    sym_to_opt = {o.streamer_symbol: o for o in options[:max_candidates]}

    greeks: dict[str, float] = {}

    async with DXLinkStreamer(session) as streamer:
        await streamer.subscribe(Greeks, symbols)

        # gather until we have all or timeout
        async def _gather():
            async for g in streamer.listen(Greeks):
                if g.event_symbol in sym_to_opt and g.delta is not None:
                    greeks[g.event_symbol] = float(g.delta)
                if len(greeks) >= len(symbols):
                    return

        try:
            await asyncio.wait_for(_gather(), timeout=2.5)
        except asyncio.TimeoutError:
            pass

    if not greeks:
        raise RuntimeError("No greeks received for candidates (DXLink)")

    best_sym = min(greeks.keys(), key=lambda s: abs(greeks[s] - target_delta))
    return sym_to_opt[best_sym], greeks[best_sym]


async def _scan_greeks_until_min_delta(
    session: Session,
    ordered_opts,  # near-OTM -> far-OTM
    *,
    min_abs_delta: float,
    batch_size: int,
    stop_n: int,
    timeout_s: float = 2.5,
    hard_timeout_s: float = 8.0,
    require_near_n: int = 5,  # must have these deltas before we allow stop/return
):
    deltas: dict[str, float] = {}
    subscribed_count = 0
    batches_used = 0

    async with DXLinkStreamer(session) as streamer:
        idx = 0

        while idx < len(ordered_opts):
            batch = ordered_opts[idx : idx + batch_size]
            idx += batch_size
            batches_used += 1

            batch_syms = [o.streamer_symbol for o in batch]
            subscribed_count += len(batch_syms)

            await streamer.subscribe(Greeks, batch_syms)

            required_syms = set(batch_syms[: min(require_near_n, len(batch_syms))])
            remaining = set(batch_syms)

            start = time.monotonic()
            soft_deadline = start + timeout_s
            hard_deadline = start + hard_timeout_s

            async for g in streamer.listen(Greeks):
                sym = getattr(g, "event_symbol", None)
                if sym in remaining:
                    d = getattr(g, "delta", None)
                    if d is not None:
                        deltas[sym] = float(d)
                    remaining.discard(sym)

                now = time.monotonic()

                # If we got everything, stop immediately
                if not remaining:
                    break

                # After soft timeout, we can stop only if we have the near strikes deltas
                if now >= soft_deadline and required_syms.issubset(deltas.keys()):
                    break

                # Hard stop no matter what
                if now >= hard_deadline:
                    break

            # Only evaluate stop condition if we have near-strike deltas.
            if not required_syms.issubset(deltas.keys()):
                continue

            # Stop condition: far-OTM tail of THIS batch reached <= min_abs_delta for stop_n consecutive
            tail_hits = 0
            for o in reversed(batch):
                d = deltas.get(o.streamer_symbol)
                if d is None:
                    continue
                if abs(d) <= min_abs_delta:
                    tail_hits += 1
                    if tail_hits >= stop_n:
                        return deltas, subscribed_count, batches_used
                else:
                    break

    return deltas, subscribed_count, batches_used


def _ordered_otm_options(options, *, right: str, spot: float):
    if right == "put":
        puts = [o for o in options if o.option_type == OptionType.PUT and float(o.strike_price) < spot]
        puts.sort(key=lambda o: float(o.strike_price), reverse=True)  # near spot downwards
        return puts
    else:
        calls = [o for o in options if o.option_type == OptionType.CALL and float(o.strike_price) > spot]
        calls.sort(key=lambda o: float(o.strike_price))  # near spot upwards
        return calls


def _find_long_leg(options, short_opt, width_points: float, right: str) -> object:
    short_strike = float(short_opt.strike_price)
    target = short_strike - width_points if right == "put" else short_strike + width_points

    same_right = [o for o in options if o.option_type == (OptionType.PUT if right == "put" else OptionType.CALL)]
    if not same_right:
        raise RuntimeError("No options found for long leg (same right)")

    # exact preferred; else nearest strike
    return min(same_right, key=lambda o: abs(float(o.strike_price) - target))


def _get_ivr(session: Session, symbol: str) -> Optional[float]:
    # Market metrics is the source of IVR; must use production for real data in many cases :contentReference[oaicite:5]{index=5}
    m = get_market_metrics(session, [symbol])[0]
    for attr in (
        "implied_volatility_index_rank",
        "tw_implied_volatility_index_rank",
        "tos_implied_volatility_index_rank",
    ):
        v = getattr(m, attr, None)
        if v is None:
            continue
        try:
            return float(v)
        except Exception:
            continue
    return None


def _csv_write_row(path: str, row: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not exists:
            w.writeheader()
        w.writerow(row)


def _read_csv(path: str) -> tuple[list[str], list[dict[str, str]]]:
    csv_path = Path(path)
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")
    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header = list(reader.fieldnames or [])
        rows = list(reader)
    return header, rows


def _safe_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _mean(values: list[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def settle_csv(csv_path: str, *, include_today: bool = False) -> list[dict[str, str]]:
    try:
        _, rows = _read_csv(csv_path)
    except FileNotFoundError as exc:
        rprint(f"[red]{exc}[/red]")
        return []

    if not rows:
        rprint({"csv": csv_path, "message": "CSV is empty."})
        return []

    today = datetime.now(timezone.utc).date()
    ready: list[tuple[date, dict[str, str]]] = []
    parse_errors = 0

    for row in rows:
        exp_str = row.get("exp")
        if not exp_str:
            continue
        try:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
        except ValueError:
            parse_errors += 1
            continue

        if exp_date < today or (include_today and exp_date == today):
            ready.append((exp_date, row))

    if not ready:
        rprint(
            {
                "csv": csv_path,
                "message": f"No trades ready to settle (include_today={include_today}).",
                "parse_errors": parse_errors,
            }
        )
        return []

    ready.sort(key=lambda item: (item[0], item[1].get("paper_id", "")))

    grouped = defaultdict(lambda: {"rows": 0, "qty": 0, "max_profit": 0.0, "max_loss": 0.0})
    details: list[dict[str, object]] = []

    for exp_date, row in ready:
        exp_key = exp_date.isoformat()
        data = grouped[exp_key]
        data["rows"] += 1

        qty = _safe_int(row.get("qty"))
        data["qty"] += qty

        mp = _safe_float(row.get("max_profit_usd")) or 0.0
        ml = _safe_float(row.get("max_loss_usd")) or 0.0
        data["max_profit"] += mp
        data["max_loss"] += ml

        details.append(
            {
                "paper_id": row.get("paper_id"),
                "cmd": row.get("cmd"),
                "right": row.get("right"),
                "exp": exp_key,
                "qty": qty,
                "limit_credit_points": _safe_float(row.get("limit_credit_points")),
                "width_points": _safe_float(row.get("width_points")),
            }
        )

    exp_summary = [
        {
            "exp": exp,
            "rows": data["rows"],
            "qty": data["qty"],
            "max_profit": round(data["max_profit"], 2),
            "max_loss": round(data["max_loss"], 2),
        }
        for exp, data in sorted(grouped.items())
    ]

    rprint(
        {
            "csv": csv_path,
            "ready_to_settle": len(ready),
            "expirations": exp_summary,
            "parse_errors": parse_errors,
        }
    )

    preview_n = min(10, len(details))
    rprint({"preview": details[:preview_n]})

    return [row for _, row in ready]


def print_stats(csv_path: str) -> dict:
    try:
        _, rows = _read_csv(csv_path)
    except FileNotFoundError as exc:
        rprint(f"[red]{exc}[/red]")
        return {}

    if not rows:
        rprint({"csv": csv_path, "message": "CSV is empty."})
        return {}

    per_cmd = Counter((row.get("cmd") or "unknown").lower() for row in rows)
    per_symbol = Counter((row.get("symbol") or "unknown").upper() for row in rows)

    qty_total = 0
    delta_vals: list[float] = []
    width_vals: list[float] = []
    credit_vals: list[float] = []
    max_profit_sum = 0.0
    max_loss_sum = 0.0
    notional_credit = 0.0
    first_created: Optional[str] = None
    last_created: Optional[str] = None

    for row in rows:
        qty = _safe_int(row.get("qty"))
        qty_total += qty

        delta = _safe_float(row.get("short_delta"))
        if delta is not None:
            delta_vals.append(abs(delta))

        width = _safe_float(row.get("width_points"))
        if width is not None:
            width_vals.append(width)

        credit = _safe_float(row.get("limit_credit_points"))
        if credit is not None:
            credit_vals.append(credit)
            notional_credit += credit * qty * float(INDEX_MULTIPLIER)

        mp = _safe_float(row.get("max_profit_usd"))
        if mp is not None:
            max_profit_sum += mp

        ml = _safe_float(row.get("max_loss_usd"))
        if ml is not None:
            max_loss_sum += ml

        created = row.get("created_at_utc")
        if created:
            if not first_created or created < first_created:
                first_created = created
            if not last_created or created > last_created:
                last_created = created

    stats = {
        "csv": csv_path,
        "rows": len(rows),
        "total_qty": qty_total,
        "by_command": dict(per_cmd),
        "top_symbols": per_symbol.most_common(5),
        "avg_abs_short_delta": round(_mean(delta_vals), 4) if delta_vals else None,
        "avg_width_points": round(_mean(width_vals), 2) if width_vals else None,
        "avg_credit_points": round(_mean(credit_vals), 2) if credit_vals else None,
        "total_theoretical_max_profit": round(max_profit_sum, 2),
        "total_theoretical_max_loss": round(max_loss_sum, 2),
        "notional_credit": round(notional_credit, 2),
        "created_range": {"first": first_created, "last": last_created},
    }

    rprint(stats)
    return stats


def _paper_enter(
    *,
    side: str,  # "buy" -> put credit, "sell" -> call credit
    symbol: str,
    qty: int,
    abs_delta: float,
    width_points: float,
    csv_path: str,
    max_candidates: int,
    min_abs_delta: float = DEFAULT_MIN_ABS_DELTA,
    batch_size: int = DEFAULT_BATCH_SIZE,
    stop_n: int = DEFAULT_STOP_N,
    max_scan: int = DEFAULT_MAX_SCAN,
):
    session = Session(_env("TT_CLIENT_SECRET"), _env("TT_REFRESH_TOKEN"))
    account = Account.get(session, _env("TT_ACCOUNT_NUMBER"))

    right = "put" if side == "buy" else "call"
    target_delta = -abs(abs_delta) if right == "put" else abs(abs_delta)

    chain = get_option_chain(session, symbol)  # options chain helper :contentReference[oaicite:6]{index=6}
    exp = _pick_expiration(chain, prefer_0dte=True)

    spot = _spot_mid(session, symbol)  # one-shot index mid :contentReference[oaicite:7]{index=7}
    options = list(chain[exp])

    ordered = _ordered_otm_options(options, right=right, spot=spot)
    ordered = ordered[:max_scan]  # safety cap
    if not ordered:
        raise RuntimeError("No candidate options after filtering")

    deltas, subscribed_count, batches_used = asyncio.run(
        _scan_greeks_until_min_delta(
            session,
            ordered,
            min_abs_delta=min_abs_delta,
            batch_size=batch_size,
            stop_n=stop_n,
        )
    )

    if not deltas:
        raise RuntimeError("No greeks received for candidates (DXLink)")

    # Pick short leg closest to target delta among what we streamed
    best_sym = min(deltas.keys(), key=lambda s: abs(deltas[s] - target_delta))
    short_opt = next(o for o in ordered if o.streamer_symbol == best_sym)
    short_d = deltas[best_sym]

    # Long leg is width away (or nearest available)
    long_opt = _find_long_leg(options, short_opt, width_points, right)

    # Fetch one-shot quotes for the two option symbols :contentReference[oaicite:8]{index=8}
    md_list = get_market_data_by_type(session, options=[short_opt.symbol, long_opt.symbol])
    md_map = {m.symbol: m for m in md_list}

    s_md = md_map.get(short_opt.symbol)
    l_md = md_map.get(long_opt.symbol)
    if not s_md or not l_md:
        raise RuntimeError("Could not fetch market data for selected option symbols")

    s_bid, s_ask, s_mid = _as_float(s_md.bid), _as_float(s_md.ask), _as_float(s_md.mid)
    l_bid, l_ask, l_mid = _as_float(l_md.bid), _as_float(l_md.ask), _as_float(l_md.mid)

    credit_mid = Decimal(str(s_mid - l_mid))
    limit_credit = _round_to_tick(credit_mid)  # tick rounding

    # Build vertical credit order (SELL short, BUY long)
    q = Decimal(str(qty))
    legs = [
        short_opt.build_leg(q, OrderAction.SELL_TO_OPEN),
        long_opt.build_leg(q, OrderAction.BUY_TO_OPEN),
    ]

    # SDK uses sign to infer credit/debit (positive=credit, negative=debit) :contentReference[oaicite:10]{index=10}
    order = NewOrder(
        time_in_force=OrderTimeInForce.DAY,
        order_type=OrderType.LIMIT,
        legs=legs,
        price=limit_credit,
    )

    # DRY RUN DEFAULT: returns BP + fees without placing :contentReference[oaicite:11]{index=11}
    try:
        resp = account.place_order(session, order, dry_run=True)
        bpe = getattr(resp, "buying_power_effect", None)
        fees = getattr(resp, "fee_calculation", None)
        errors = getattr(resp, "errors", None)
        warnings = getattr(resp, "warnings", None)
    except Exception as exc:
        rprint(f"[yellow]Dry run failed (margin/API error): {exc}[/yellow]")
        rprint("[yellow]Logging trade anyway with empty BP/fee data.[/yellow]")
        resp = None
        bpe = None
        fees = None
        errors = [{"code": "DRY_RUN_EXCEPTION", "message": str(exc)}]
        warnings = None

    ivr = _get_ivr(session, symbol)

    # Paper P/L boundaries (defined risk)
    width = Decimal(str(width_points))
    max_profit = limit_credit * INDEX_MULTIPLIER * q
    max_loss = (width - limit_credit) * INDEX_MULTIPLIER * q

    row = {
        "paper_id": str(uuid.uuid4()),
        "created_at_utc": _now_utc(),
        "cmd": side,
        "symbol": symbol,
        "right": right,
        "exp": str(exp),
        "qty": qty,
        "target_abs_delta": abs_delta,
        "short_delta": short_d,
        "width_points": width_points,
        "spot_mid": float(spot),
        "short_strike": float(short_opt.strike_price),
        "long_strike": float(long_opt.strike_price),
        "short_bid": s_bid,
        "short_ask": s_ask,
        "short_mid": s_mid,
        "long_bid": l_bid,
        "long_ask": l_ask,
        "long_mid": l_mid,
        "credit_mid_points": float(credit_mid),
        "limit_credit_points": float(limit_credit),
        "max_profit_usd": float(max_profit),
        "max_loss_usd": float(max_loss),
        "ivr": ivr,
        "bp_change": float(getattr(bpe, "change_in_buying_power", 0) or 0) if bpe else None,
        "bp_isolated_margin_req": float(getattr(bpe, "isolated_order_margin_requirement", 0) or 0) if bpe else None,
        "fees_total": float(getattr(fees, "total_fees", 0) or 0) if fees else None,
        "fees_breakdown": json.dumps(fees.model_dump(), default=str) if fees else None,
        "dry_run_warnings": json.dumps([w.model_dump() for w in warnings], default=str) if warnings else None,
        "dry_run_errors": json.dumps(errors, default=str) if errors else None,
        "greeks_subscribed": subscribed_count,
        "greeks_batches": batches_used,
        "greeks_received": len(deltas),
    }

    _csv_write_row(csv_path, row)

    rprint(
        {
            "paper_id": row["paper_id"],
            "cmd": row["cmd"],
            "symbol": row["symbol"],
            "exp": row["exp"],
            "strikes": f'{row["short_strike"]}/{row["long_strike"]}',
            "short_delta": row["short_delta"],
            "limit_credit_points": row["limit_credit_points"],
            "max_loss_usd": row["max_loss_usd"],
            "ivr": row["ivr"],
            "bp_change": row["bp_change"],
            "fees_total": row["fees_total"],
            "logged_to": csv_path,
        }
    )


@app.command()
def buy(
    symbol: str = typer.Option(DEFAULT_SYMBOL),
    qty: int = typer.Option(DEFAULT_QTY),
    delta: float = typer.Option(DEFAULT_ABS_DELTA),
    width: float = typer.Option(DEFAULT_WIDTH_POINTS),
    csv_path: str = typer.Option(CSV_DEFAULT),
    max_candidates: int = typer.Option(180),
    min_abs_delta: float = typer.Option(DEFAULT_MIN_ABS_DELTA),
    batch_size: int = typer.Option(DEFAULT_BATCH_SIZE),
    stop_n: int = typer.Option(DEFAULT_STOP_N),
    max_scan: int = typer.Option(DEFAULT_MAX_SCAN),
):
    """Paper-enter SPX PUT credit spread (bullish). Dry-run BP/fees + CSV log by default."""
    _paper_enter(
        side="buy",
        symbol=symbol,
        qty=qty,
        abs_delta=delta,
        width_points=width,
        csv_path=csv_path,
        max_candidates=max_candidates,
        min_abs_delta=min_abs_delta,
        batch_size=batch_size,
        stop_n=stop_n,
        max_scan=max_scan,
    )


@app.command()
def sell(
    symbol: str = typer.Option(DEFAULT_SYMBOL),
    qty: int = typer.Option(DEFAULT_QTY),
    delta: float = typer.Option(DEFAULT_ABS_DELTA),
    width: float = typer.Option(DEFAULT_WIDTH_POINTS),
    csv_path: str = typer.Option(CSV_DEFAULT),
    max_candidates: int = typer.Option(180),
    min_abs_delta: float = typer.Option(DEFAULT_MIN_ABS_DELTA),
    batch_size: int = typer.Option(DEFAULT_BATCH_SIZE),
    stop_n: int = typer.Option(DEFAULT_STOP_N),
    max_scan: int = typer.Option(DEFAULT_MAX_SCAN),
):
    """Paper-enter SPX CALL credit spread (bearish). Dry-run BP/fees + CSV log by default."""
    _paper_enter(
        side="sell",
        symbol=symbol,
        qty=qty,
        abs_delta=delta,
        width_points=width,
        csv_path=csv_path,
        max_candidates=max_candidates,
        min_abs_delta=min_abs_delta,
        batch_size=batch_size,
        stop_n=stop_n,
        max_scan=max_scan,
    )


@app.command()
def settle(
    csv_path: str = typer.Option("./paper_trades.csv", "--csv-path"),
    include_today: bool = typer.Option(False, "--include-today"),
    exp: str | None = typer.Option(None, "--exp"),
    settlement: float | None = typer.Option(None, "--settlement"),
    source: str = typer.Option("yahoo", "--source"),
    apply: bool = typer.Option(False, "--apply", help="Write settlement results back into the CSV"),
):
    # keep your existing preview/listing output here if you want

    if not apply:
        rprint({"csv": csv_path, "note": "preview-only (use --apply to write results)"})
        return

    n = settle_file(
        csv_path=csv_path,
        source=source,
        exp_filter=exp,
        settlement_override=settlement,
        include_today=include_today,
    )
    rprint({"csv": csv_path, "settled_rows": n})


@app.command()
def stats(csv_path: str = "./paper_trades.csv"):
    """Print aggregate stats about logged paper trades."""
    print_stats(csv_path)
if __name__ == "__main__":
    app()
