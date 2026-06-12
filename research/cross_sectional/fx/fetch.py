"""
G10 FX data fetcher for the cross-sectional FX book (phase F0).

Three FREE, no-API-key public sources. Each external URL and the download date
are documented inline. Output is cached idempotently into ./data as raw CSVs
(gitignored by the repo `data/` rule — raw CSVs are NOT committed).

Sources
-------
1. SPOT (daily close)  — primary: Stooq free CSV
     https://stooq.com/q/d/l/?s=<sym>&i=d           (downloaded 2026-06-12)
   Stooq now gates the CSV endpoint behind a SHA-256 proof-of-work browser
   challenge AND an "Access denied" block on automated/data-center IPs for the
   /q/d/l/ download path specifically. We implement the PoW solver (works from a
   non-blocked IP) and AUTOMATICALLY FALL BACK to Yahoo Finance when Stooq
   returns the "Access denied" body, so the loader is reproducible everywhere.
     fallback: Yahoo Finance chart API
     https://query1.finance.yahoo.com/v8/finance/chart/<sym>?range=25y&interval=1d
                                                          (downloaded 2026-06-12)

2. SHORT RATES (carry)  — primary: FRED free CSV, OECD 3-month interbank rate:
     https://fred.stlouisfed.org/graph/fredgraph.csv?id=IR3TIB01<CC>M156N
                                                          (downloaded 2026-06-12)
   One uniform concept for all 9 + USD (see FRED_SERIES below), % p.a., monthly.
   FRED was egress-blocked (http=000) from this sandbox mid-session, so we
   AUTOMATICALLY FALL BACK to the OECD SDMX REST API, which republishes the SAME
   IR3TIB (3-month interbank, % p.a.) series from a different, reachable host:
     fallback: https://sdmx.oecd.org/public/rest/data/
               OECD.SDD.STES,DSD_STES@DF_FINMARK,4.0/<AREA>.M.IR3TIB......?...
                                                          (downloaded 2026-06-12)
   Monthly source either way, forward-filled to the daily grid in fxdata.py.

3. REER (value)  — BIS SDMX REST CSV, Monthly / Real / Broad effective FX rate:
     https://stats.bis.org/api/v1/data/BIS,WS_EER,1.0/M.R.B.<AREA>?format=csv
                                                          (downloaded 2026-06-12)
   Real broad REER index (64-economy basket). Monthly, forward-filled to daily.
   (BIS also offers a bulk zip https://data.bis.org/static/bulk/WS_EER_csv_row.zip
    but the per-series SDMX CSV above is cleaner, so we use it.)
"""

import hashlib
import re
import time
from pathlib import Path

import pandas as pd
import requests

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"
TIMEOUT = 30
SLEEP = 0.3  # polite gap between requests

# ── Universe: 9 non-USD G10 vs USD (USD is the numeraire) ─────────────────────
# All normalized to XXXUSD orientation = "USD per 1 unit of foreign currency"
# (price up => foreign currency strengthens vs USD).
CURRENCIES = ["EUR", "JPY", "GBP", "CHF", "AUD", "NZD", "CAD", "NOK", "SEK"]

# Stooq FX symbols (lowercase, no separator). Stooq quotes some as USDxxx.
STOOQ_SYMBOL = {
    "EUR": "eurusd", "JPY": "usdjpy", "GBP": "gbpusd", "CHF": "usdchf",
    "AUD": "audusd", "NZD": "nzdusd", "CAD": "usdcad", "NOK": "usdnok",
    "SEK": "usdsek",
}

# Yahoo Finance FX symbols. "XXXUSD=X" is USD-per-XXX (already correct);
# bare "XXX=X" is USD-base (USD per... no — it is XXX per USD), must INVERT.
YAHOO_SYMBOL = {
    "EUR": "EURUSD=X", "JPY": "JPY=X", "GBP": "GBPUSD=X", "CHF": "CHF=X",
    "AUD": "AUDUSD=X", "NZD": "NZDUSD=X", "CAD": "CAD=X", "NOK": "NOK=X",
    "SEK": "SEK=X",
}

# Per-currency inversion needed to reach the XXXUSD ("USD per foreign unit")
# orientation. True  => raw source is USDXXX (foreign per USD) -> take 1/price.
# False => raw source is already XXXUSD (USD per foreign).
#   Stooq:  EUR/GBP/AUD/NZD already XXXUSD; JPY/CHF/CAD/NOK/SEK are USDxxx.
#   Yahoo:  EUR/GBP/AUD/NZD use "XXXUSD=X" (no invert); JPY/CHF/CAD/NOK/SEK use
#           "XXX=X" which is USD-base (foreign per USD) -> invert.
# The two source maps happen to agree on which currencies need inversion.
INVERT = {
    "EUR": False, "JPY": True, "GBP": False, "CHF": True, "AUD": False,
    "NZD": False, "CAD": True, "NOK": True, "SEK": True,
}

# FRED short-rate series (carry leg): OECD 3-month interbank rate, % per annum.
# Uniform concept across all 10 (USD is the numeraire leg used as the subtrahend
# in the rate differential foreign - USD). Series id pattern IR3TIB01<CC>M156N.
#                              (downloaded 2026-06-12)
FRED_SERIES = {
    "USD": "IR3TIB01USM156N",  # United States   3M interbank, % p.a.
    "EUR": "IR3TIB01EZM156N",  # Euro area
    "JPY": "IR3TIB01JPM156N",  # Japan
    "GBP": "IR3TIB01GBM156N",  # United Kingdom
    "CHF": "IR3TIB01CHM156N",  # Switzerland
    "AUD": "IR3TIB01AUM156N",  # Australia
    "NZD": "IR3TIB01NZM156N",  # New Zealand
    "CAD": "IR3TIB01CAM156N",  # Canada
    "NOK": "IR3TIB01NOM156N",  # Norway
    "SEK": "IR3TIB01SEM156N",  # Sweden
}

# OECD SDMX REF_AREA codes (FRED fallback). Same IR3TIB 3M interbank concept,
# % per annum. EUR uses the euro-area aggregate EA20.       (downloaded 2026-06-12)
OECD_AREA = {
    "USD": "USA", "EUR": "EA20", "JPY": "JPN", "GBP": "GBR", "CHF": "CHE",
    "AUD": "AUS", "NZD": "NZL", "CAD": "CAN", "NOK": "NOR", "SEK": "SWE",
}

# BIS SDMX REF_AREA codes for REER (Monthly, Real, Broad).
BIS_AREA = {
    "EUR": "XM", "JPY": "JP", "GBP": "GB", "CHF": "CH", "AUD": "AU",
    "NZD": "NZ", "CAD": "CA", "NOK": "NO", "SEK": "SE",
}


# ── Spot: Stooq (primary, PoW-gated) with Yahoo fallback ──────────────────────

def _stooq_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    return s


def _solve_stooq_pow(session: requests.Session, html: str) -> bool:
    """Solve Stooq's SHA-256 proof-of-work challenge and POST /__verify to obtain
    the access cookie. Challenge: find n s.t. sha256(c+str(n)) starts with d hex
    zeros. Returns True if a challenge was found and submitted.
    https://stooq.com  (challenge observed 2026-06-12)"""
    m = re.search(r'c="([^"]+)"', html)
    d_m = re.search(r"d=(\d+)", html)
    if not m or not d_m:
        return False
    c, d = m.group(1), int(d_m.group(1))
    target = "0" * d
    n = 0
    while not hashlib.sha256((c + str(n)).encode()).hexdigest().startswith(target):
        n += 1
    session.post("https://stooq.com/__verify", data={"c": c, "n": n}, timeout=TIMEOUT)
    return True


def fetch_spot_stooq(ccy: str, session: requests.Session | None = None) -> pd.DataFrame:
    """Daily OHLCV CSV from Stooq -> DataFrame[date, close] (raw orientation).
    https://stooq.com/q/d/l/?s=<sym>&i=d   (downloaded 2026-06-12)
    Returns empty DataFrame on the "Access denied" data-center block so the
    caller can fall back to Yahoo."""
    s = session or _stooq_session()
    sym = STOOQ_SYMBOL[ccy]
    url = f"https://stooq.com/q/d/l/?s={sym}&i=d"
    r = s.get(url, timeout=TIMEOUT)
    if "c=" in r.text[:1000] and "__verify" in r.text:
        _solve_stooq_pow(s, r.text)
        r = s.get(url, timeout=TIMEOUT)
    body = r.text.strip()
    if not body.lower().startswith("date") or "access denied" in body.lower():
        # blocked / quota / not CSV
        return pd.DataFrame()
    from io import StringIO
    df = pd.read_csv(StringIO(body))
    if "Date" not in df.columns or "Close" not in df.columns:
        return pd.DataFrame()
    out = pd.DataFrame({
        "date": pd.to_datetime(df["Date"], utc=True),
        "close": df["Close"].astype(float),
    })
    return out.dropna().sort_values("date").reset_index(drop=True)


def fetch_spot_yahoo(ccy: str, session: requests.Session | None = None) -> pd.DataFrame:
    """Daily close from Yahoo Finance chart API -> DataFrame[date, close] (raw).
    https://query1.finance.yahoo.com/v8/finance/chart/<sym>?range=25y&interval=1d
    (downloaded 2026-06-12)"""
    s = session or requests.Session()
    s.headers.update({"User-Agent": UA})
    sym = YAHOO_SYMBOL[ccy]
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
    r = s.get(url, params={"range": "25y", "interval": "1d"}, timeout=TIMEOUT)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    ts = res["timestamp"]
    close = res["indicators"]["quote"][0]["close"]
    out = pd.DataFrame({
        "date": pd.to_datetime(ts, unit="s", utc=True).normalize(),
        "close": pd.Series(close, dtype="float64"),
    })
    return out.dropna().sort_values("date").reset_index(drop=True)


def fetch_spot(ccy: str, session: requests.Session | None = None) -> tuple[pd.DataFrame, str]:
    """Stooq first, Yahoo fallback. Returns (df[date,close], source_used)."""
    try:
        df = fetch_spot_stooq(ccy, session)
        if not df.empty:
            return df, "stooq"
        print(f"  spot {ccy}: Stooq returned no CSV (access denied / blocked) "
              f"-> falling back to Yahoo")
    except Exception as e:
        print(f"  spot {ccy}: Stooq failed ({type(e).__name__}: {e}) -> Yahoo")
    df = fetch_spot_yahoo(ccy)
    return df, "yahoo"


# ── Short rates: FRED ─────────────────────────────────────────────────────────

def fetch_rate_fred(ccy: str, session: requests.Session | None = None) -> pd.DataFrame:
    """3M interbank rate (% p.a.) from FRED -> DataFrame[date, rate].
    https://fred.stlouisfed.org/graph/fredgraph.csv?id=<id>  (downloaded 2026-06-12)
    Uses requests (not pandas' http reader, which stalls without fsspec/aiohttp).
    Short connect timeout so a blocked host fails fast into the OECD fallback."""
    from io import StringIO
    s = session or requests.Session()
    s.headers.update({"User-Agent": UA})
    sid = FRED_SERIES[ccy]
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
    r = s.get(url, timeout=(8, 20))
    r.raise_for_status()
    df = pd.read_csv(StringIO(r.text))
    df.columns = ["date", "rate"]
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df["rate"] = pd.to_numeric(df["rate"], errors="coerce")
    return df.dropna().sort_values("date").reset_index(drop=True)


def fetch_rate_oecd(ccy: str, session: requests.Session | None = None) -> pd.DataFrame:
    """Same IR3TIB 3M interbank rate (% p.a.) from the OECD SDMX REST API.
    https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_FINMARK,4.0/
        <AREA>.M.IR3TIB......?startPeriod=1990-01&...&format=csvfile
    (downloaded 2026-06-12)  -> DataFrame[date, rate], TIME_PERIOD anchored to
    month-start (matches the FRED monthly convention)."""
    from io import StringIO
    s = session or requests.Session()
    s.headers.update({"User-Agent": UA})
    area = OECD_AREA[ccy]
    url = (f"https://sdmx.oecd.org/public/rest/data/"
           f"OECD.SDD.STES,DSD_STES@DF_FINMARK,4.0/{area}.M.IR3TIB......"
           f"?startPeriod=1990-01&dimensionAtObservation=AllDimensions&format=csvfile")
    r = s.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    raw = pd.read_csv(StringIO(r.text))
    df = pd.DataFrame({
        "date": pd.to_datetime(raw["TIME_PERIOD"], format="%Y-%m", utc=True),
        "rate": pd.to_numeric(raw["OBS_VALUE"], errors="coerce"),
    })
    return df.dropna().sort_values("date").reset_index(drop=True)


def fetch_rate(ccy: str, session: requests.Session | None = None) -> tuple[pd.DataFrame, str]:
    """FRED first, OECD SDMX fallback. Returns (df[date,rate], source_used).
    Both serve the identical IR3TIB 3-month interbank rate, % p.a."""
    try:
        df = fetch_rate_fred(ccy, session)
        if not df.empty:
            return df, "fred"
    except Exception as e:
        print(f"  rate {ccy}: FRED failed ({type(e).__name__}) -> OECD SDMX")
    return fetch_rate_oecd(ccy), "oecd"


# ── REER: BIS SDMX ────────────────────────────────────────────────────────────

def fetch_reer(ccy: str, session: requests.Session | None = None) -> pd.DataFrame:
    """Monthly Real Broad REER index from BIS SDMX REST CSV -> DataFrame[date, reer].
    https://stats.bis.org/api/v1/data/BIS,WS_EER,1.0/M.R.B.<AREA>?format=csv
    (downloaded 2026-06-12)"""
    s = session or requests.Session()
    s.headers.update({"User-Agent": UA})
    area = BIS_AREA[ccy]
    url = f"https://stats.bis.org/api/v1/data/BIS,WS_EER,1.0/M.R.B.{area}?format=csv"
    r = s.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    from io import StringIO
    raw = pd.read_csv(StringIO(r.text))
    df = pd.DataFrame({
        # TIME_PERIOD is YYYY-MM; anchor each month to its first day.
        "date": pd.to_datetime(raw["TIME_PERIOD"], format="%Y-%m", utc=True),
        "reer": pd.to_numeric(raw["OBS_VALUE"], errors="coerce"),
    })
    return df.dropna().sort_values("date").reset_index(drop=True)


# ── Idempotent cache ──────────────────────────────────────────────────────────

def _path(kind: str, ccy: str) -> Path:
    return DATA_DIR / f"{kind}_{ccy}.csv"


def fetch_all(refresh: bool = False) -> None:
    """Download + cache spot, rate and REER for the full universe (USD rate too).
    Skips files that already exist unless refresh=True."""
    print("=== FETCH: spot (Stooq->Yahoo) ===")
    spot_sess = _stooq_session()
    for ccy in CURRENCIES:
        p = _path("spot", ccy)
        if p.exists() and not refresh:
            continue
        df, src = fetch_spot(ccy, spot_sess)
        if df.empty:
            print(f"  spot {ccy}: FAILED (no data from any source)")
            continue
        df.to_csv(p, index=False)
        print(f"  spot {ccy}: {len(df)} rows via {src} -> {p.name}")
        time.sleep(SLEEP)

    print("=== FETCH: short rates (FRED -> OECD) ===")
    rate_sess = requests.Session()
    rate_sess.headers.update({"User-Agent": UA})
    for ccy in ["USD", *CURRENCIES]:
        p = _path("rate", ccy)
        if p.exists() and not refresh:
            continue
        try:
            df, src = fetch_rate(ccy, rate_sess)
            df.to_csv(p, index=False)
            tag = FRED_SERIES[ccy] if src == "fred" else f"OECD {OECD_AREA[ccy]}"
            print(f"  rate {ccy}: {len(df)} rows via {src} ({tag}) -> {p.name}")
        except Exception as e:
            print(f"  rate {ccy}: FAILED ({type(e).__name__}: {e})")
        time.sleep(SLEEP)

    print("=== FETCH: REER (BIS) ===")
    bis_sess = requests.Session()
    bis_sess.headers.update({"User-Agent": UA})
    for ccy in CURRENCIES:
        p = _path("reer", ccy)
        if p.exists() and not refresh:
            continue
        try:
            df = fetch_reer(ccy, bis_sess)
            df.to_csv(p, index=False)
            print(f"  reer {ccy}: {len(df)} rows (BIS {BIS_AREA[ccy]}) -> {p.name}")
        except Exception as e:
            print(f"  reer {ccy}: FAILED BIS {BIS_AREA[ccy]} ({type(e).__name__}: {e})")
        time.sleep(SLEEP)


if __name__ == "__main__":
    import sys
    fetch_all(refresh="--refresh" in sys.argv)
