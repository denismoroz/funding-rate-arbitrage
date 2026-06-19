"""Shared HTTP helpers for XSMOM venue probes (urllib only, no requests dep)."""
import json
import time
import urllib.request
import urllib.error

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# The 32-coin XSMOM universe
UNIVERSE = ["AAVE", "ADA", "APT", "ARB", "ATOM", "AVAX", "BCH", "BNB", "BTC",
            "CRV", "DOGE", "DOT", "EIGEN", "ENA", "ETH", "INJ", "JTO", "JUP",
            "LINK", "LTC", "NEAR", "PENDLE", "PYTH", "SOL", "SUI", "TAO", "TRX",
            "UNI", "WLD", "XLM", "XRP", "ZRO"]

# thin alts to probe depth on + liquid refs
THIN_ALTS = ["CRV", "JTO", "JUP", "PYTH", "ZRO", "EIGEN", "PENDLE", "WLD", "INJ", "TAO"]
DEPTH_PROBE = THIN_ALTS + ["BTC", "ETH", "SOL"]


def http_get(url, headers=None, timeout=25):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def http_post(url, payload, headers=None, timeout=25):
    h = {"User-Agent": UA, "Accept": "application/json",
         "Content-Type": "application/json"}
    if headers:
        h.update(headers)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def get_json(url, headers=None, timeout=25, retries=2):
    last = None
    for i in range(retries + 1):
        try:
            return json.loads(http_get(url, headers, timeout))
        except Exception as e:
            last = e
            time.sleep(0.6 * (i + 1))
    raise last


def post_json(url, payload, headers=None, timeout=25, retries=2):
    last = None
    for i in range(retries + 1):
        try:
            return json.loads(http_post(url, payload, headers, timeout))
        except Exception as e:
            last = e
            time.sleep(0.6 * (i + 1))
    raise last


def book_metrics(bids, asks, mid=None):
    """bids/asks = list of (price, size). Returns (spread_bps, depth_1pct_usd).
    depth_1pct_usd = notional USD resting within 1% of mid on BOTH sides summed."""
    if not bids or not asks:
        return None, 0.0
    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])
    if mid is None:
        mid = (best_bid + best_ask) / 2.0
    if mid <= 0:
        return None, 0.0
    spread_bps = (best_ask - best_bid) / mid * 1e4
    lo = mid * 0.99
    hi = mid * 1.01
    depth = 0.0
    for p, s in bids:
        p = float(p); s = float(s)
        if p >= lo:
            depth += p * s
    for p, s in asks:
        p = float(p); s = float(s)
        if p <= hi:
            depth += p * s
    return spread_bps, depth


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
