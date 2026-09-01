"""Hit kite.profile() endpoint directly to see the raw HTTP response body
(Kite SDK often hides extra fields from the server's JSON error)."""
import requests
import config

headers = {
    "X-Kite-Version": "3",
    "Authorization": f"token {config.KITE_API_KEY}:{config.KITE_ACCESS_TOKEN}",
}
r = requests.get("https://api.kite.trade/user/profile", headers=headers)
print(f"HTTP {r.status_code}")
print("Response body:")
print(r.text)
