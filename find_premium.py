"""
find_premium.py
---------------
Scan live Kite option chains for SENSEX / BANKNIFTY / NIFTY and find
contracts trading near a target premium (default ₹60).

Run:
    python find_premium.py                       # default target ₹60
    python find_premium.py --premium 50          # target ₹50
    python find_premium.py --underlying SENSEX   # just one underlying
    python find_premium.py --tolerance 30        # ₹60 ± ₹30
"""

import argparse
import sys
import warnings

warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

from kiteconnect import KiteConnect
import config

EXCHANGE_FOR = {
    "NIFTY": "NFO",
    "BANKNIFTY": "NFO",
    "SENSEX": "BFO",
}

LOT_SIZE = {
    "NIFTY": 75,
    "BANKNIFTY": 30,
    "SENSEX": 20,
}


def make_kite():
    k = KiteConnect(api_key=config.KITE_API_KEY)
    k.set_access_token(config.KITE_ACCESS_TOKEN)
    k.profile()
    return k


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--premium", type=float, default=60.0,
                   help="Target premium in Rs (default 60)")
    p.add_argument("--tolerance", type=float, default=25.0,
                   help="±Rs tolerance around target (default 25)")
    p.add_argument("--underlying", choices=["NIFTY", "BANKNIFTY", "SENSEX"],
                   default=None, help="Restrict to one underlying")
    p.add_argument("--capital", type=float, default=4000.0,
                   help="Capital available — flags contracts that fit (default 4000)")
    args = p.parse_args()

    target = args.premium
    tol = args.tolerance
    low, high = target - tol, target + tol

    print()
    print("=" * 78)
    print(f"  Searching option chains for contracts at premium "
          f"Rs {low:.0f} - Rs {high:.0f}  (target Rs {target:.0f})")
    print(f"  Capital: Rs {args.capital:,.0f}  |  marking contracts whose "
          f"lot-cost fits 95% of capital")
    print("=" * 78)

    kite = make_kite()

    underlyings = ([args.underlying] if args.underlying
                   else ["SENSEX", "BANKNIFTY", "NIFTY"])
    cache = {}

    for u in underlyings:
        exch = EXCHANGE_FOR[u]
        lot = LOT_SIZE[u]
        if exch not in cache:
            print(f"\n[fetching] {exch} instruments dump...", end=" ", flush=True)
            cache[exch] = kite.instruments(exch)
            print(f"  {len(cache[exch])} instruments")
        instruments = cache[exch]

        # Filter to this underlying's options
        opts = [
            i for i in instruments
            if i.get("name") == u
            and i.get("instrument_type") in ("CE", "PE")
            and i.get("expiry")
        ]
        if not opts:
            print(f"\n{u}: no options found in {exch}")
            continue

        # Soonest expiry
        from datetime import date
        today = date.today()
        future_exps = sorted({i["expiry"] for i in opts if i["expiry"] >= today})
        if not future_exps:
            print(f"\n{u}: no future expiries")
            continue
        nearest = future_exps[0]
        same_exp = [i for i in opts if i["expiry"] == nearest]

        # Sort strikes by spot proximity (we don't have spot yet; fetch one quote)
        # Pick a sample of strikes (every 4th) to avoid hammering quote API
        all_strikes = sorted({float(i["strike"]) for i in same_exp})
        sample = all_strikes[::max(1, len(all_strikes) // 30)]

        print(f"\n{u} (lot={lot})  expiry={nearest}  "
              f"({len(all_strikes)} strikes, sampling {len(sample)})")

        # Batch quotes — Kite allows up to ~500 instruments per quote call
        keys_ce = [f"{exch}:{i['tradingsymbol']}" for i in same_exp
                    if float(i["strike"]) in sample and i["instrument_type"] == "CE"]
        keys_pe = [f"{exch}:{i['tradingsymbol']}" for i in same_exp
                    if float(i["strike"]) in sample and i["instrument_type"] == "PE"]
        all_keys = keys_ce + keys_pe
        # Chunked just in case
        prices = {}
        for chunk_start in range(0, len(all_keys), 200):
            chunk = all_keys[chunk_start:chunk_start + 200]
            try:
                q = kite.quote(chunk)
                for k, v in q.items():
                    prices[k] = float(v["last_price"])
            except Exception as e:
                print(f"  quote error: {e}")

        # Build a map back to contract metadata
        by_key = {f"{exch}:{i['tradingsymbol']}": i for i in same_exp}

        # Find matches in premium range
        matches = []
        for key, prem in prices.items():
            if low <= prem <= high:
                meta = by_key.get(key)
                if not meta:
                    continue
                lot_cost = prem * lot
                fits = lot_cost <= args.capital * 0.95
                matches.append((prem, lot_cost, fits, meta))

        if not matches:
            print(f"  (no contracts in Rs {low:.0f}-{high:.0f} range)")
            continue

        # Sort by closeness to target, then print top 5
        matches.sort(key=lambda x: abs(x[0] - target))
        print(f"  {'tradingsymbol':<28} {'strike':>9} {'type':>4} "
              f"{'premium':>9} {'lot cost':>10} fits ₹{args.capital:.0f}?")
        for prem, lot_cost, fits, meta in matches[:8]:
            mark = "✅" if fits else "❌"
            print(f"  {meta['tradingsymbol']:<28} "
                  f"{int(meta['strike']):>9} {meta['instrument_type']:>4} "
                  f"Rs {prem:>6.2f}  Rs {lot_cost:>6.0f}    {mark}")

    print()
    print("=" * 78)
    print("  Use the tradingsymbol shown above to look up the contract in Kite Web.")
    print("  The bot's --small-capital mode auto-picks one of these when signals fire.")
    print("=" * 78)


if __name__ == "__main__":
    main()
