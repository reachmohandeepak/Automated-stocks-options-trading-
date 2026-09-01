"""
diag_kite.py
------------
Minimal Kite auth diagnostic. Tests:
  1. .env values load cleanly (no stray whitespace / quotes)
  2. profile() endpoint — confirms api_key+access_token are valid as a pair
  3. margins() endpoint — confirms account-level access
  4. instruments() — confirms read-only market data access
  5. historical_data() — confirms historical data subscription
  6. Reports which subset of these failed, narrowing the cause.
"""

from kiteconnect import KiteConnect
import config

print("=" * 60)
print("Loaded .env values (masked):")
ak = config.KITE_API_KEY
sk = config.KITE_API_SECRET
at = config.KITE_ACCESS_TOKEN
print(f"  API_KEY     : '{ak}'  len={len(ak)}  repr={repr(ak)}")
print(f"  API_SECRET  : {'*' * len(sk)}  len={len(sk)}")
print(f"  ACCESS_TOKEN: '{at[:4]}...{at[-4:]}'  len={len(at)}  repr_ends={repr(at[-6:])}")
print("=" * 60)

kite = KiteConnect(api_key=ak)
kite.set_access_token(at)

def try_call(name, fn):
    try:
        r = fn()
        print(f"  [OK]    {name}")
        return True, r
    except Exception as e:
        print(f"  [FAIL]  {name}: {e}")
        return False, None

print("\nTesting endpoints in order of permission scope:\n")
p_ok, profile = try_call("profile()", kite.profile)
m_ok, _       = try_call("margins()", kite.margins)
i_ok, instr   = try_call("instruments('NSE')", lambda: kite.instruments("NSE"))

# Only try historical if we have basic auth working AND can resolve a token
if i_ok and instr:
    token = next((x["instrument_token"] for x in instr if x["tradingsymbol"] == "RELIANCE"), None)
    if token:
        from datetime import datetime, timedelta
        import pytz
        IST = pytz.timezone("Asia/Kolkata")
        to_dt = datetime.now(IST)
        from_dt = to_dt - timedelta(days=2)
        try_call(
            "historical_data(RELIANCE, 5minute, 2d)",
            lambda: kite.historical_data(token, from_dt, to_dt, "5minute"),
        )

print("\n" + "=" * 60)
if p_ok:
    print("Profile says:", profile.get("user_name"), "|", profile.get("user_shortname"))
print("Done.")
