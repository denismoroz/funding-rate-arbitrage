"""
F2 TRUST GATE — validate OUR FX factor CONSTRUCTION against AQR's published free
monthly CURRENCY factor returns.

WHY: before F3 issues a verdict on the FX book, we must know our carry/momentum/
value LEGS are built correctly (right sign, right leg, right orientation). The
cheapest external ground-truth is AQR's free academic data library. We correlate
OUR monthly factor returns against AQR's published currency (FX) factor returns.

AQR SOURCES (free, no API key; URLs verified live + cached on the date below)
----------------------------------------------------------------------------
  Value & Momentum Everywhere: Factors, Monthly   (Asness/Moskowitz/Pedersen 2013)
    page : https://www.aqr.com/Insights/Datasets/Value-and-Momentum-Everywhere-Factors-Monthly
    file : https://www.aqr.com/-/media/AQR/Documents/Insights/Data-Sets/Value-and-Momentum-Everywhere-Factors-Monthly.xlsx
    downloaded: 2026-06-12 ; cached: data/aqr_vme_factors_monthly.xlsx
    The "VME Factors" sheet (header at row 22) publishes per-asset-class long/short
    VAL and MOM excess returns. The CURRENCIES (FX) columns are:
        VALLS_VME_FX  -> AQR currency VALUE  factor (our `value`)
        MOMLS_VME_FX  -> AQR currency MOMENTUM factor (our `momentum`)
    Monthly, month-end dated MM/DD/YYYY, FX legs span ~1979 -> 2025 (554 months).

  Currency CARRY: AQR's free "Carry" (Koijen/Moskowitz/Pedersen/Vrugt) dataset is
    NOT reachable as a parseable file from this environment — the dedicated dataset
    page 404s and the guessable /-/media/.../Carry.xlsx path returns an HTML
    landing page (not the workbook). VME does NOT contain an FX carry factor. So
    carry is reported as NO-AQR-SOURCE (not fabricated). Value & Momentum ARE
    cross-checked; carry's construction is left for a later re-run if AQR exposes a
    free currency-carry file.

xlsx WITHOUT openpyxl: openpyxl is NOT importable in this venv and the brief forbids
adding deps. An .xlsx is an OOXML zip of XML, so we read it with the stdlib
(zipfile + xml.etree + sharedStrings) — no new dependency. We do NOT attempt the
old binary .xls path (would need xlrd).

OUR monthly factor returns: each single-factor book's DAILY net pnl from
FXXSecPackage.menu (carry/momentum/value) resampled to MONTH-END COMPOUNDED returns
(1+r).prod()-1, then aligned to AQR's month-end dates on the overlapping window.

GATE (per factor): Pearson corr(our monthly, AQR monthly) over the common window.
  corr > 0.7            -> PASS  (construction validated)
  0.4 <= corr <= 0.7    -> BORDERLINE (likely universe/definition diff — review)
  corr < 0.4 / negative -> FLAG a probable construction bug (sign/leg/orientation)
Also report sign-agreement rate and n_months overlap.

EGRESS HONESTY: external downloads here are flaky. If the AQR source is unreachable/
unparseable we DO NOT fabricate a correlation — we print
"AQR SOURCE UNREACHABLE — GATE INCONCLUSIVE", still save OUR monthly returns (so the
check is reproducible later), and exit non-fatally (exit 0). The code runs correctly
from an unblocked IP.

numpy/pandas + stdlib only.
"""

from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pandas as pd

import requests

from fx_pkg import FXXSecPackage

_HERE = Path(__file__).parent
DATA_DIR = _HERE / "data"
DATA_DIR.mkdir(exist_ok=True)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"
TIMEOUT = 30

VME_URL = ("https://www.aqr.com/-/media/AQR/Documents/Insights/Data-Sets/"
           "Value-and-Momentum-Everywhere-Factors-Monthly.xlsx")
VME_CACHE = DATA_DIR / "aqr_vme_factors_monthly.xlsx"
VME_SHEET = "xl/worksheets/sheet1.xml"      # "VME Factors" is the 1st sheet
VME_HEADER_ROW = 22                          # row with DATE / VALLS_VME_FX / ...

# AQR column header -> our menu factor name. (carry has no free AQR FX source.)
AQR_MAP = {
    "VALLS_VME_FX": "value",
    "MOMLS_VME_FX": "momentum",
}

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


# ── minimal stdlib xlsx reader (no openpyxl) ─────────────────────────────────────

def _download_vme() -> bool:
    """Fetch the VME xlsx into the cache. Returns True iff we now hold a valid
    OOXML zip (a blocked/redirected fetch yields HTML, not a zip → False)."""
    if VME_CACHE.exists() and zipfile.is_zipfile(VME_CACHE):
        return True
    try:
        r = requests.get(VME_URL, headers={"User-Agent": UA}, timeout=TIMEOUT)
    except Exception as e:
        print(f"  [egress] GET failed: {type(e).__name__}: {e}")
        return False
    if r.status_code != 200:
        print(f"  [egress] HTTP {r.status_code} from AQR")
        return False
    VME_CACHE.write_bytes(r.content)
    if not zipfile.is_zipfile(VME_CACHE):
        head = r.content[:64].lstrip()[:40]
        print(f"  [egress] response is not an xlsx zip (got {head!r}…) — blocked/HTML")
        return False
    return True


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    sst: list[str] = []
    for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(NS + "si"):
        sst.append("".join(t.text or "" for t in si.iter(NS + "t")))
    return sst


def _cell_value(c, sst: list[str]):
    t = c.get("t")
    v = c.find(NS + "v")
    if v is None:
        isn = c.find(NS + "is")
        return "".join(x.text or "" for x in isn.iter(NS + "t")) if isn is not None else None
    return sst[int(v.text)] if t == "s" else v.text


def _read_vme_fx() -> pd.DataFrame:
    """Parse the cached VME xlsx → DataFrame[month-end date x {value, momentum}]
    of AQR currency-factor monthly returns. Raises on a structural mismatch."""
    z = zipfile.ZipFile(VME_CACHE)
    sst = _shared_strings(z)
    sd = ET.fromstring(z.read(VME_SHEET)).find(NS + "sheetData")

    rows_by_n: dict[int, dict[str, object]] = {}
    for r in sd.findall(NS + "row"):
        cells: dict[str, object] = {}
        for c in r.findall(NS + "c"):
            col = re.match(r"[A-Z]+", c.get("r")).group()
            cells[col] = _cell_value(c, sst)
        rows_by_n[int(r.get("r"))] = cells

    header = rows_by_n.get(VME_HEADER_ROW, {})
    # map AQR header label -> spreadsheet column letter
    col_of = {label: col for col, label in header.items() if label}
    if "DATE" not in col_of or any(a not in col_of for a in AQR_MAP):
        raise ValueError(f"VME header row {VME_HEADER_ROW} missing expected columns; "
                         f"found {sorted(set(header.values()))[:30]}")
    date_col = col_of["DATE"]
    fx_cols = {aqr: col_of[aqr] for aqr in AQR_MAP}

    recs: dict[str, list] = {"date": [], **{aqr: [] for aqr in AQR_MAP}}
    for rn in sorted(rows_by_n):
        if rn <= VME_HEADER_ROW:
            continue
        cells = rows_by_n[rn]
        dval = cells.get(date_col)
        if not dval:
            continue
        vals = {aqr: cells.get(c) for aqr, c in fx_cols.items()}
        if all(v in (None, "") for v in vals.values()):
            continue  # FX legs not yet populated (early history)
        recs["date"].append(dval)
        for aqr in AQR_MAP:
            v = vals[aqr]
            recs[aqr].append(float(v) if v not in (None, "") else np.nan)

    df = pd.DataFrame(recs)
    df["date"] = pd.to_datetime(df["date"], format="%m/%d/%Y", utc=True)
    df = df.set_index("date").sort_index()
    # rename AQR columns to OUR factor names
    df = df.rename(columns=AQR_MAP)
    return df  # columns: value, momentum (month-end monthly returns)


# ── OUR monthly factor returns ───────────────────────────────────────────────────

def _our_monthly_factors() -> pd.DataFrame:
    """Daily single-factor book pnl → month-end compounded monthly returns.

    (1+r).prod()-1 per calendar month, indexed at month-end (UTC) to align with
    AQR's month-end stamps."""
    pkg = FXXSecPackage()
    menu = pkg.menu("XSEC", pkg.load("XSEC"))
    cols = {}
    for f in ("carry", "momentum", "value"):
        daily = menu[f].dropna()
        monthly = (1.0 + daily).resample("ME").prod() - 1.0
        cols[f] = monthly
    out = pd.DataFrame(cols)
    out.index = out.index.tz_convert("UTC") if out.index.tz else out.index.tz_localize("UTC")
    return out


def _align_month_end(ours: pd.Series, theirs: pd.Series) -> pd.DataFrame:
    """Align two month-end monthly series on the (year, month) key (robust to a
    day-of-month mismatch between ME-resample and AQR's exact calendar month-end)."""
    a = ours.copy(); a.index = a.index.tz_localize(None).to_period("M")
    b = theirs.copy(); b.index = b.index.tz_localize(None).to_period("M")
    j = pd.concat([a.rename("ours"), b.rename("aqr")], axis=1, join="inner").dropna()
    return j


def _gate(corr: float) -> str:
    if np.isnan(corr):
        return "NO-DATA"
    if corr > 0.7:
        return "PASS ✅"
    if corr >= 0.4:
        return "BORDERLINE ⚠️ (review: likely universe/definition diff)"
    return "FLAG ❌ (probable construction bug: sign/leg/orientation)"


# ── main ─────────────────────────────────────────────────────────────────────────

def main() -> None:
    print("#" * 72)
    print("##### F2 AQR cross-check — OUR FX factors vs AQR free monthly currency #####")
    print("#" * 72)

    ours = _our_monthly_factors()
    # Always persist OUR monthly returns so the check is reproducible offline.
    ours_out = DATA_DIR / "our_monthly_factor_returns.csv"
    ours.to_csv(ours_out)
    print(f"OUR monthly factor returns: {len(ours)} months "
          f"{ours.index.min().date()} -> {ours.index.max().date()}  "
          f"(saved → data/{ours_out.name})")

    print(f"\nFetching AQR VME factors:\n  {VME_URL}")
    if not _download_vme():
        print("\n" + "=" * 72)
        print("AQR SOURCE UNREACHABLE — GATE INCONCLUSIVE (re-run from an unblocked IP)")
        print("=" * 72)
        print("OUR monthly factor returns ARE saved above; the gate can be completed")
        print("later by re-running this script from an IP with AQR egress.")
        return  # non-fatal

    try:
        aqr = _read_vme_fx()
    except Exception as e:
        print(f"\n  [parse] failed to parse VME xlsx: {type(e).__name__}: {e}")
        print("\n" + "=" * 72)
        print("AQR FILE UNPARSEABLE — GATE INCONCLUSIVE (re-run / inspect cache)")
        print("=" * 72)
        return  # non-fatal

    print(f"AQR VME FX factors: {len(aqr)} months "
          f"{aqr.index.min().date()} -> {aqr.index.max().date()}  "
          f"(cached → data/{VME_CACHE.name})")
    print("  AQR column mapping:  VALLS_VME_FX → value   MOMLS_VME_FX → momentum")
    print("  carry: NO free AQR currency-carry file reachable → not cross-checked")

    print("\n" + "=" * 72)
    print(f"{'factor':<10}{'n_months':>9}{'window':>20}{'corr':>8}"
          f"{'sign_agree':>12}   gate")
    print("-" * 72)
    results = {}
    for f in ("carry", "momentum", "value"):
        if f not in aqr.columns:
            print(f"{f:<10}{'—':>9}{'(no AQR source)':>20}{'—':>8}{'—':>12}   "
                  f"NO-AQR-SOURCE")
            results[f] = {"corr": None, "n_months": 0, "note": "no AQR free source"}
            continue
        j = _align_month_end(ours[f], aqr[f])
        if len(j) < 24:
            print(f"{f:<10}{len(j):>9}{'too short':>20}{'—':>8}{'—':>12}   INSUFFICIENT")
            results[f] = {"corr": None, "n_months": int(len(j))}
            continue
        corr = float(j["ours"].corr(j["aqr"]))
        sign_agree = float((np.sign(j["ours"]) == np.sign(j["aqr"])).mean())
        win = f"{j.index.min()}..{j.index.max()}"
        print(f"{f:<10}{len(j):>9}{win:>20}{corr:>+8.3f}{sign_agree:>11.1%}   "
              f"{_gate(corr)}")
        results[f] = {"corr": corr, "n_months": int(len(j)),
                      "sign_agree": sign_agree,
                      "window": [str(j.index.min()), str(j.index.max())]}
    print("=" * 72)

    # persist the gate result alongside our returns
    import json
    (DATA_DIR / "aqr_crosscheck_result.json").write_text(
        json.dumps({"map": AQR_MAP, "results": results}, indent=2))
    print(f"gate result → data/aqr_crosscheck_result.json")

    flags = [f for f, r in results.items()
             if r.get("corr") is not None and r["corr"] < 0.4]
    if flags:
        print(f"\n!! REVIEW: {flags} corr < 0.4 — probable construction bug, "
              f"fix BEFORE the F3 verdict.")


if __name__ == "__main__":
    main()
