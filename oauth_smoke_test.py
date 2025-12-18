#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from dotenv import load_dotenv

from tastytrade import Session, Account


def require(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v


def main() -> int:
    load_dotenv()

    client_secret = require("TT_CLIENT_SECRET")
    refresh_token = require("TT_REFRESH_TOKEN")
    is_test = os.getenv("TT_IS_TEST", "0").strip() in ("1", "true", "TRUE", "yes", "YES")

    # 1) OAuth login (session token derived from refresh token)
    session = Session(client_secret, refresh_token, is_test=is_test)

    # 2) Fetch accounts (proves auth works)
    accounts = Account.get(session)
    if not accounts:
        raise RuntimeError("No accounts returned. Auth may have succeeded, but account list is empty.")

    env_acct = os.getenv("TT_ACCOUNT_NUMBER")
    acct_num = env_acct or accounts[0].account_number

    # 3) Fetch balances for a specific account (proves account-scoped access works)
    account = Account.get(session, acct_num)
    bal = account.get_balances(session)

    def safe_get(obj, attr: str):
        return getattr(obj, attr, None)

    print("OK: OAuth session + account access succeeded")
    print(f"  env: {'SANDBOX' if is_test else 'PROD'}")
    print(f"  account_number: {acct_num}")
    print(f"  net_liquidating_value: {safe_get(bal, 'net_liquidating_value')}")
    print(f"  cash_balance: {safe_get(bal, 'cash_balance')}")
    print(f"  equity_buying_power: {safe_get(bal, 'equity_buying_power')}")
    print(f"  derivative_buying_power: {safe_get(bal, 'derivative_buying_power')}")

    # 4) Optional: force refresh and re-check one endpoint (validates refresh path)
    session.refresh()
    _ = Account.get(session)
    print("OK: session.refresh() succeeded")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"FAIL: {e}", file=sys.stderr)
        raise SystemExit(1)
