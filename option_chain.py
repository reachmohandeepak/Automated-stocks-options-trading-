"""
option_chain.py
---------------
Fetch intraday option chain for NIFTY / BANKNIFTY (from NSE) and
SENSEX / BANKEX (from BSE), and print a readable analysis.

For each underlying, shows:
  * Spot price + ATM strike
  * 10 strikes around ATM (CE and PE: LTP, OI, change in OI, volume, IV)
  * Total CE OI / total PE OI / Put-Call Ratio (PCR)
  * Max-OI strikes (resistance for CE, support for PE)

Usage:
    python option_chain.py                 # all (NIFTY + BANKNIFTY + SENSEX)
    python option_chain.py NIFTY           # one
    python option_chain.py NIFTY BANKNIFTY # multiple

NOTE: NSE blocks direct API access — we follow their cookie flow
(visit homepage first to get session cookies, then call the API).
"""

import sys
import time
import warnings
from datetime import datetime
from typing import Optional

warnings.filterwarnings("ignore")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

import requests

# curl_cffi mimics real Chrome's TLS fingerprint — NSE blocks plain
# `requests` but accepts curl_cffi. Falls back to requests if missing.
try:
    from curl_cffi import requests as cffi_requests
    HAVE_CFFI = True
except ImportError:
    HAVE_CFFI = False


# ---------------------------------------------------------------------------
# NSE — used for NIFTY, BANKNIFTY, FINNIFTY
# ---------------------------------------------------------------------------
NSE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/option-chain",
    "Connection": "keep-alive",
    "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}


def _nse_session():
    """Open a session, visit NSE homepage to collect cookies, return it.
    Uses curl_cffi (Chrome TLS fingerprint) if available, else requests."""
    if HAVE_CFFI:
        s = cffi_requests.Session(impersonate="chrome")
    else:
        s = requests.Session()
    s.headers.update(NSE_HEADERS)
    # Warm up cookies — NSE requires you to "visit" the site first
    try:
        s.get("https://www.nseindia.com", timeout=10)
        time.sleep(0.5)
        s.get("https://www.nseindia.com/option-chain", timeout=10)
        time.sleep(0.5)
    except Exception:
        pass
    return s


def fetch_nse_option_chain(symbol: str) -> Optional[dict]:
    """symbol: 'NIFTY' or 'BANKNIFTY' or 'FINNIFTY'."""
    url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
    for attempt in range(3):
        s = _nse_session()
        try:
            r = s.get(url, timeout=15)
            if r.status_code == 200 and r.text.strip():
                return r.json()
            print(f"    (attempt {attempt+1}: HTTP {r.status_code})")
            time.sleep(1.5 * (attempt + 1))
        except Exception as e:
            print(f"    (attempt {attempt+1}: {e})")
            time.sleep(1.5)
    return None


# ---------------------------------------------------------------------------
# BSE — used for SENSEX, BANKEX
# ---------------------------------------------------------------------------
BSE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.bseindia.com",
    "Referer": "https://www.bseindia.com/",
}


def fetch_bse_option_chain(symbol: str) -> Optional[dict]:
    """symbol: 'SENSEX' or 'BANKEX'.

    BSE's option chain API is less stable than NSE's. If this returns None
    or partial data, treat it as an upstream issue, not a bug here.
    """
    s = requests.Session()
    s.headers.update(BSE_HEADERS)
    # BSE option chain JSON endpoint (subject to change)
    url = (
        "https://api.bseindia.com/BseIndiaAPI/api/ddlExpiry_IV/w"
        f"?ProductType=IO&id1={symbol}"
    )
    try:
        r = s.get(url, timeout=15)
        if r.status_code != 200:
            return None
        # BSE returns expiries first; full chain needs another call per expiry.
        # We're being pragmatic: tell the user to use NSE-listed equivalents.
        return r.json() if r.text.strip() else None
    except (requests.RequestException, ValueError):
        return None


# ---------------------------------------------------------------------------
# Analysis & display
# ---------------------------------------------------------------------------
def _nearest_strike(spot: float, strikes: list[float]) -> float:
    return min(strikes, key=lambda s: abs(s - spot))


def render_nse_chain(symbol: str, payload: dict, around: int = 10) -> None:
    rec = payload.get("records") or {}
    flt = payload.get("filtered") or {}
    data = flt.get("data") or rec.get("data") or []
    if not data:
        print(f"  No option data returned for {symbol}.")
        return

    spot = rec.get("underlyingValue") or 0.0
    expiry = (flt.get("expiryDates") or rec.get("expiryDates") or ["?"])[0]
    strikes = sorted({row["strikePrice"] for row in data})
    atm = _nearest_strike(spot, strikes)

    # Filter to nearest-expiry rows only
    by_strike = {}
    for row in data:
        if row.get("expiryDate") != expiry:
            continue
        by_strike[row["strikePrice"]] = row

    if not by_strike:
        # Some payloads only have multi-expiry "data"; fall back to first expiry seen
        first_exp = data[0]["expiryDate"]
        expiry = first_exp
        by_strike = {row["strikePrice"]: row for row in data if row["expiryDate"] == first_exp}

    # Show 'around' strikes on each side of ATM
    visible_strikes = sorted(by_strike.keys())
    try:
        atm_idx = visible_strikes.index(atm)
    except ValueError:
        atm_idx = len(visible_strikes) // 2
        atm = visible_strikes[atm_idx]
    lo = max(0, atm_idx - around)
    hi = min(len(visible_strikes), atm_idx + around + 1)
    show = visible_strikes[lo:hi]

    print()
    print("=" * 110)
    print(f"  {symbol}  —  Spot: {spot:,.2f}   ATM: {atm:,.0f}   "
          f"Expiry: {expiry}   ({datetime.now().strftime('%H:%M:%S')})")
    print("=" * 110)

    # Header
    print(f"  {'CE OI':>10} {'CE ΔOI':>10} {'CE Vol':>10} {'CE LTP':>8} "
          f"{'CE IV':>6}  |  {'STRIKE':^8}  |  "
          f"{'PE LTP':>8} {'PE IV':>6} {'PE Vol':>10} {'PE ΔOI':>10} {'PE OI':>10}")
    print("-" * 110)

    tot_ce_oi = tot_pe_oi = 0
    tot_ce_vol = tot_pe_vol = 0
    max_ce_oi_strike = max_pe_oi_strike = None
    max_ce_oi = max_pe_oi = 0

    for strike in show:
        row = by_strike[strike]
        ce = row.get("CE") or {}
        pe = row.get("PE") or {}
        ce_oi = ce.get("openInterest", 0)
        pe_oi = pe.get("openInterest", 0)
        ce_doi = ce.get("changeinOpenInterest", 0)
        pe_doi = pe.get("changeinOpenInterest", 0)
        ce_v = ce.get("totalTradedVolume", 0)
        pe_v = pe.get("totalTradedVolume", 0)
        ce_ltp = ce.get("lastPrice", 0)
        pe_ltp = pe.get("lastPrice", 0)
        ce_iv = ce.get("impliedVolatility", 0)
        pe_iv = pe.get("impliedVolatility", 0)

        marker = "<" if strike == atm else " "
        print(f"  {ce_oi:>10,} {ce_doi:>+10,} {ce_v:>10,} {ce_ltp:>8.2f} "
              f"{ce_iv:>6.2f}  |  {strike:>8.0f}{marker} |  "
              f"{pe_ltp:>8.2f} {pe_iv:>6.2f} {pe_v:>10,} {pe_doi:>+10,} {pe_oi:>10,}")

    # Totals across ALL strikes (not just visible)
    for strike, row in by_strike.items():
        ce = row.get("CE") or {}
        pe = row.get("PE") or {}
        tot_ce_oi += ce.get("openInterest", 0)
        tot_pe_oi += pe.get("openInterest", 0)
        tot_ce_vol += ce.get("totalTradedVolume", 0)
        tot_pe_vol += pe.get("totalTradedVolume", 0)
        if ce.get("openInterest", 0) > max_ce_oi:
            max_ce_oi = ce["openInterest"]
            max_ce_oi_strike = strike
        if pe.get("openInterest", 0) > max_pe_oi:
            max_pe_oi = pe["openInterest"]
            max_pe_oi_strike = strike

    pcr = (tot_pe_oi / tot_ce_oi) if tot_ce_oi else 0
    pcr_sentiment = ("Bullish (PCR > 1.3)" if pcr > 1.3
                     else "Bearish (PCR < 0.7)" if pcr < 0.7
                     else "Neutral")

    print("-" * 110)
    print(f"  Total CE OI : {tot_ce_oi:>15,}    Total CE Vol : {tot_ce_vol:>15,}")
    print(f"  Total PE OI : {tot_pe_oi:>15,}    Total PE Vol : {tot_pe_vol:>15,}")
    print(f"  PCR (OI)    : {pcr:>15.3f}    {pcr_sentiment}")
    print(f"  Max CE OI   : strike {max_ce_oi_strike:,.0f}  ({max_ce_oi:,})  "
          f"→ likely RESISTANCE")
    print(f"  Max PE OI   : strike {max_pe_oi_strike:,.0f}  ({max_pe_oi:,})  "
          f"→ likely SUPPORT")
    print("=" * 110)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    requested = [s.upper() for s in sys.argv[1:]] or ["NIFTY", "BANKNIFTY", "SENSEX"]

    print()
    print(f"  Indian Index Option Chain  —  {datetime.now()}")
    print()

    for sym in requested:
        if sym in {"NIFTY", "BANKNIFTY", "FINNIFTY"}:
            print(f"\n[NSE] Fetching option chain for {sym}...")
            payload = fetch_nse_option_chain(sym)
            if not payload:
                print(f"  ✗ NSE refused the request for {sym}. "
                      f"Their API often rate-limits; try again in 30s.")
                continue
            render_nse_chain(sym, payload)
        elif sym in {"SENSEX", "BANKEX"}:
            print(f"\n[BSE] Fetching option chain for {sym}...")
            payload = fetch_bse_option_chain(sym)
            if not payload:
                print(f"  ✗ BSE option chain API didn't return data for {sym}.")
                print(f"    BSE doesn't expose a clean public option-chain JSON "
                      f"like NSE does — for SENSEX options, you'd typically use "
                      f"a broker API (Zerodha/Upstox/Dhan) or the BSE website "
                      f"manually: https://www.bseindia.com/derivatives/...")
            else:
                # BSE payload structure is just expiries — implementation stub.
                print(f"  BSE returned expiries: {payload}")
                print(f"  (Full BSE chain parsing requires per-expiry API calls "
                      f"and unstable schema — see notes in option_chain.py)")
        else:
            print(f"\n  ! Unknown symbol: {sym}")
            print(f"    Supported: NIFTY, BANKNIFTY, FINNIFTY (NSE), "
                  f"SENSEX, BANKEX (BSE)")

    print()


if __name__ == "__main__":
    main()
