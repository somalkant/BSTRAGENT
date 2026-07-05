"""
Kite Connect authentication helper.

Kite Connect access tokens expire daily. This module handles:
  1. Generating the login URL (paste into browser)
  2. Exchanging the request_token (from redirect URL) for an access_token
  3. Saving/loading access_token to avoid re-login mid-session
  4. Auto-detecting if today's token is already saved
"""

from __future__ import annotations

import os
import json
from datetime import date
from pathlib import Path
from dotenv import load_dotenv
from kiteconnect import KiteConnect

load_dotenv()

TOKEN_CACHE = Path(__file__).parent.parent / "checkpoints" / "access_token.json"

API_KEY    = os.getenv("KITE_API_KEY")
API_SECRET = os.getenv("KITE_API_SECRET")


def get_kite() -> KiteConnect:
    """
    Returns an authenticated KiteConnect instance.
    Uses cached token if today's token exists, otherwise raises LoginRequired.
    Call login_step1() + login_step2() first if this raises an error.
    """
    kite = KiteConnect(api_key=API_KEY)
    token_data = _load_cached_token()

    if token_data and token_data.get("date") == str(date.today()):
        kite.set_access_token(token_data["access_token"])
        return kite

    raise LoginRequired(
        "No valid access token for today. Run login_step1() then login_step2()."
    )


def login_step1() -> str:
    """
    Step 1: Generate the Kite login URL.
    Returns the URL — paste it into your browser, log in,
    then copy the request_token from the redirect URL.

    Redirect URL will look like:
      https://127.0.0.1/?request_token=XXXXXXXXXX&action=login&status=success
    Copy the value of request_token.
    """
    kite = KiteConnect(api_key=API_KEY)
    url = kite.login_url()
    print("=" * 60)
    print("STEP 1: Open this URL in your browser and log in:")
    print()
    print(url)
    print()
    print("After login you'll be redirected to a URL like:")
    print("  https://127.0.0.1/?request_token=XXXXX&action=login&status=success")
    print()
    print("Copy the request_token value and pass it to login_step2()")
    print("=" * 60)
    return url


def login_step2(request_token: str) -> KiteConnect:
    """
    Step 2: Exchange request_token for access_token.
    Saves token to checkpoints/access_token.json (valid for today only).
    Returns authenticated KiteConnect instance ready to use.
    """
    kite = KiteConnect(api_key=API_KEY)
    session = kite.generate_session(request_token, api_secret=API_SECRET)
    access_token = session["access_token"]

    kite.set_access_token(access_token)
    _save_token(access_token)

    print(f"Login successful. Access token saved for {date.today()}.")
    print("You can now use get_kite() for the rest of this session.")
    return kite


def _save_token(access_token: str) -> None:
    TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(TOKEN_CACHE, "w") as f:
        json.dump({"date": str(date.today()), "access_token": access_token}, f)


def _load_cached_token() -> dict | None:
    if not TOKEN_CACHE.exists():
        return None
    with open(TOKEN_CACHE) as f:
        return json.load(f)


class LoginRequired(Exception):
    pass
