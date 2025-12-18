#!/usr/bin/env python3
"""
Minimal correctness tests for settlement math.

Run:
  python test_settlement_math.py
"""

import math
from paper_settle import payoff_points_vertical, compute_pnl_usd, MULTIPLIER

def assert_close(a, b, tol=1e-9):
    if abs(a - b) > tol:
        raise AssertionError(f"{a} != {b} (tol={tol})")

def test_put_vertical_payoff():
    # Put spread strikes 4000/3995 (width 5)
    k1, k2 = 4000.0, 3995.0

    # Above high strike => 0 payoff
    assert_close(payoff_points_vertical(4010.0, k1, k2, "put"), 0.0)

    # Between => payoff = K_high - S
    assert_close(payoff_points_vertical(3998.0, k1, k2, "put"), 2.0)

    # Below low => width payoff
    assert_close(payoff_points_vertical(3980.0, k1, k2, "put"), 5.0)

def test_call_vertical_payoff():
    # Call spread strikes 4000/4005 (width 5)
    k1, k2 = 4000.0, 4005.0

    # Below low => 0 payoff
    assert_close(payoff_points_vertical(3990.0, k1, k2, "call"), 0.0)

    # Between => payoff = S - K_low
    assert_close(payoff_points_vertical(4003.0, k1, k2, "call"), 3.0)

    # Above high => width payoff
    assert_close(payoff_points_vertical(4010.0, k1, k2, "call"), 5.0)

def test_pnl():
    credit = 1.00
    payoff = 0.25
    qty = 2
    fees = -3.50
    gross, net = compute_pnl_usd(credit, payoff, qty, fees)
    assert_close(gross, (credit - payoff) * MULTIPLIER * qty)
    assert_close(net, gross + fees)

if __name__ == "__main__":
    test_put_vertical_payoff()
    test_call_vertical_payoff()
    test_pnl()
    print("OK")
