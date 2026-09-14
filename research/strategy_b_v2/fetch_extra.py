"""Data for new hedge-signal families: Deribit implied volatility (DVOL) and macro risk-off (FRED).

DVOL = Deribit's 30-day implied volatility index, hourly, BTC and ETH, from 2021-03 (option market's
forward-looking fear gauge). FRED: VIXCLS (equity implied vol), SP500, DTWEXBGS (dollar index), daily.
"""
import io, sys, time
from pathlib import Path
import pandas as pd, requests

OUT = Path("/private/tmp/claude-501/-Users-d-prj-funding-rate-arbitrage/5f1f1c94-f80e-4bdd-88c5-68cfed37b7b4/scratchpad/b_extra")
START, END = 1614556800000, int(time.time() // 3600 * 3600 * 1000)      # 2021-03-01 .. now


def dvol(currency):
    rows, t = [], START
    while t < END:
        hi = min(t + 720 * 3600_000, END)
        r = requests.get("https://www.deribit.com/api/v2/public/get_volatility_index_data",
                         params=dict(currency=currency, start_timestamp=t, end_timestamp=hi, resolution=3600), timeout=30)
        data = r.json().get("result", {}).get("data", [])
        rows += [(c[0], c[4]) for c in data]
        t = hi
        time.sleep(0.1)
    df = pd.DataFrame(rows, columns=["ms", "dvol"]).drop_duplicates("ms").sort_values("ms")
    df["time"] = pd.to_datetime(df["ms"], unit="ms", utc=True)
    return df[["time", "dvol"]]


def fred(series):
    r = requests.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}", timeout=30)
    df = pd.read_csv(io.StringIO(r.text)).rename(columns={"observation_date": "time", series: "value"})
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df[df.value.astype(str).str.replace(".", "", 1).str.isdigit()].assign(value=lambda d: d.value.astype(float))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for c in ("BTC", "ETH"):
        df = dvol(c)
        df.to_csv(OUT / f"dvol_{c}.csv", index=False)
        print(f"DVOL {c}: {len(df)} hours {df.time.min()} .. {df.time.max()}", flush=True)
    for s in ("VIXCLS", "SP500", "DTWEXBGS"):
        df = fred(s)
        df.to_csv(OUT / f"{s}.csv", index=False)
        print(f"{s}: {len(df)} days {df.time.min().date()} .. {df.time.max().date()}", flush=True)


if __name__ == "__main__":
    main()
