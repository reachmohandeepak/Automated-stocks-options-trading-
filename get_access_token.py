"""
get_access_token.py
-------------------
One-shot helper to generate a fresh Kite Connect access_token.
Run this each trading day before starting main.py — Kite tokens expire
every day at ~07:30 IST.

Usage:
    python get_access_token.py

Flow:
    1. Script prints a Kite login URL.
    2. Open it in your browser and log in with your Zerodha credentials.
    3. After login, the browser redirects to your registered redirect URL
       with `?request_token=XYZ` in the address bar. Copy that token.
    4. Paste it back into this script when prompted.
    5. Script prints the access_token AND patches it into .env automatically.
"""

import os
import re
import sys

from kiteconnect import KiteConnect

import config


def patch_env_file(path: str, key: str, value: str) -> bool:
    """Rewrite a single KEY=VALUE line in a .env file. Returns True if updated."""
    if not os.path.exists(path):
        return False
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    pattern = re.compile(rf"^{re.escape(key)}=.*$")
    updated = False
    for i, line in enumerate(lines):
        if pattern.match(line.strip()):
            lines[i] = f"{key}={value}\n"
            updated = True
            break
    if not updated:
        # Key not present — append
        lines.append(f"{key}={value}\n")
        updated = True
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return updated


def main() -> None:
    api_key = config.KITE_API_KEY
    api_secret = config.KITE_API_SECRET

    if not api_key or not api_secret:
        sys.exit("KITE_API_KEY and KITE_API_SECRET must be set in .env first.")

    kite = KiteConnect(api_key=api_key)

    print("=" * 70)
    print("1) Open this URL in your browser and log in to Zerodha:")
    print()
    print("   " + kite.login_url())
    print()
    print("2) After login, your browser will redirect to a URL like:")
    print("   https://YOUR_REDIRECT/?request_token=XXXXXXX&action=login&status=success")
    print()
    print("3) Copy the value of `request_token` from that URL and paste below.")
    print("=" * 70)

    request_token = input("\nrequest_token: ").strip()
    if not request_token:
        sys.exit("No request_token provided. Aborting.")

    try:
        session = kite.generate_session(request_token, api_secret=api_secret)
    except Exception as e:
        sys.exit(f"Failed to generate session: {e}")

    access_token = session["access_token"]
    print("\nAccess token generated:")
    print(f"  {access_token}")

    if patch_env_file(".env", "KITE_ACCESS_TOKEN", access_token):
        print("\n.env updated — KITE_ACCESS_TOKEN written.")
    else:
        print("\nCould not patch .env automatically. Add this line manually:")
        print(f"  KITE_ACCESS_TOKEN={access_token}")

    print("\nYou can now run:  python main.py")


if __name__ == "__main__":
    main()
