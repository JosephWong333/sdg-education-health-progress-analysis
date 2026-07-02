"""
Pull official SDG indicator data for the ISS Phase 2 project. VERSION 2.
Joseph Wong, ISS Summer 2026

What changed from v1 (based on inspecting the v1 output):
  1. Maternal mortality series code fixed: SH_STA_MMR returned nothing,
     the correct UNSD code is SH_STA_MORT (SDG 3.1.1).
  2. Dimension filtering added. The UNSD database reports some series
     broken down by sex or location (MALE/FEMALE, URBAN/RURAL). v1
     averaged those together, which inflated electricity access for
     low-access countries. v2 drops disaggregated rows and keeps only
     totals (both sexes / all areas).
  3. If a series returns zero rows, the script now searches the live
     catalogue for likely codes and prints them, instead of failing quietly.

How to run:
  pip install requests pandas
  python pull_sdg_api_data_v2.py             (pulls everything, writes CSV)
  python pull_sdg_api_data_v2.py --selftest  (tests the parsers offline)

Output: sdg_official_data.csv (same columns as before, plus maternal).
"""

import sys
import time

import requests
import pandas as pd

UNSD_BASE = "https://unstats.un.org/sdgs/UNSDGAPIV5/v1/sdg"
WB_BASE = "https://api.worldbank.org/v2"

COUNTRIES = ["Nigeria", "Ethiopia", "Rwanda", "India", "Ghana", "Bangladesh"]
ISO3 = {"Nigeria": "NGA", "Ethiopia": "ETH", "Rwanda": "RWA",
        "India": "IND", "Ghana": "GHA", "Bangladesh": "BGD"}
YEARS = (2000, 2023)

UNSD_SERIES = {
    "SH_DYN_MORT": "under5_mortality_per_1000",    # SDG 3.2.1
    "SH_STA_MORT": "maternal_mortality_per_100k",  # SDG 3.1.1 (fixed in v2)
    "EG_ACS_ELEC": "electricity_access_pct",       # SDG 7.1.1
}
SEARCH_HINT = {  # used to auto-suggest codes if a series comes back empty
    "SH_DYN_MORT": "under-five",
    "SH_STA_MORT": "maternal",
    "EG_ACS_ELEC": "electricity",
}
WB_INDICATORS = {
    "SE.PRM.NENR": "net_enrollment_primary_pct",
}

# v2: reject rows that are disaggregations rather than totals
DISAGGREGATED = {"MALE", "FEMALE", "URBAN", "RURAL"}

HEADERS = {"User-Agent": "ISS-intern-project/2.0 (educational use)"}


def list_series(keyword=""):
    """Browse the UNSD series catalogue. Use this to confirm series codes."""
    r = requests.get(f"{UNSD_BASE}/Series/List", headers=HEADERS, timeout=60)
    r.raise_for_status()
    hits = []
    for s in r.json():
        text = f"{s.get('code', '')} {s.get('description', '')}"
        if keyword.lower() in text.lower():
            hits.append((s.get("code"), s.get("description")))
    return hits


def is_total_row(d):
    """v2: keep only totals. A row is dropped if any of its dimension
    values is a known disaggregation (by sex or by location)."""
    dims = d.get("dimensions") or {}
    return not any(str(v).upper() in DISAGGREGATED for v in dims.values())


def parse_unsd_payload(payload, wanted_geos=None, y0=YEARS[0], y1=YEARS[1]):
    """Turn one page of a UNSD /Series/Data response into tidy rows."""
    out = []
    for d in payload.get("data", []):
        geo = d.get("geoAreaName")
        if wanted_geos and geo not in wanted_geos:
            continue
        if not is_total_row(d):
            continue
        try:
            year = int(d.get("timePeriodStart"))
            value = float(d.get("value"))
        except (TypeError, ValueError):
            continue
        if not (y0 <= year <= y1):
            continue
        out.append({"country": geo, "year": year,
                    "series": d.get("seriesCode"), "value": value})
    return out


def get_unsd_series(series_code):
    rows, page = [], 1
    while True:
        r = requests.get(f"{UNSD_BASE}/Series/Data",
                         params={"seriesCode": series_code,
                                 "pageSize": 500, "page": page},
                         headers=HEADERS, timeout=120)
        r.raise_for_status()
        payload = r.json()
        rows += parse_unsd_payload(payload, wanted_geos=set(COUNTRIES))
        total_pages = payload.get("totalPages")
        if total_pages is not None and page >= int(total_pages):
            break
        if not payload.get("data"):
            break
        page += 1
        time.sleep(0.4)
    return rows


def parse_wb_payload(payload):
    out = []
    if not isinstance(payload, list) or len(payload) < 2 or not payload[1]:
        return out
    for d in payload[1]:
        try:
            year = int(d.get("date"))
            value = float(d.get("value"))
        except (TypeError, ValueError):
            continue
        iso = d.get("countryiso3code") or (d.get("country") or {}).get("id")
        out.append({"iso3": iso, "year": year, "value": value})
    return out


def get_wb_indicator(code):
    iso_list = ";".join(ISO3.values())
    url = (f"{WB_BASE}/country/{iso_list}/indicator/{code}"
           f"?format=json&per_page=1000&date={YEARS[0]}:{YEARS[1]}")
    r = requests.get(url, headers=HEADERS, timeout=120)
    r.raise_for_status()
    return parse_wb_payload(r.json())


def main():
    frames = []
    iso_to_country = {v: k for k, v in ISO3.items()}

    for code, colname in UNSD_SERIES.items():
        print(f"Pulling UNSD series {code} ...")
        rows = get_unsd_series(code)
        if not rows:
            print(f"  WARNING: no rows for {code}. Likely codes from the "
                  f"live catalogue:")
            for c, desc in list_series(SEARCH_HINT.get(code, ""))[:5]:
                print(f"    {c}: {desc}")
            continue
        df = pd.DataFrame(rows)[["country", "year", "value"]]
        df = df.rename(columns={"value": colname})
        # after dimension filtering, duplicates per country-year should be
        # rare; average whatever remains as a final safety
        df = df.groupby(["country", "year"], as_index=False).mean(numeric_only=True)
        frames.append(df)
        print(f"  got {len(df)} country-year values")

    for code, colname in WB_INDICATORS.items():
        print(f"Pulling World Bank indicator {code} ...")
        rows = get_wb_indicator(code)
        df = pd.DataFrame(rows)
        df["country"] = df["iso3"].map(iso_to_country)
        df = df.dropna(subset=["country"])[["country", "year", "value"]]
        df = df.rename(columns={"value": colname})
        frames.append(df)
        print(f"  got {len(df)} country-year values")

    merged = frames[0]
    for df in frames[1:]:
        merged = merged.merge(df, on=["country", "year"], how="outer")
    merged = merged.sort_values(["country", "year"])
    merged.to_csv("sdg_official_data.csv", index=False)
    print(f"\nWrote sdg_official_data.csv with {len(merged)} rows.")
    print("Coverage by column:")
    print(merged.count())


def selftest():
    unsd_sample = {
        "totalPages": 1,
        "data": [
            {"seriesCode": "EG_ACS_ELEC", "geoAreaName": "Rwanda",
             "timePeriodStart": 2015, "value": "22.8",
             "dimensions": {"Location": "ALLAREA"}},
            {"seriesCode": "EG_ACS_ELEC", "geoAreaName": "Rwanda",
             "timePeriodStart": 2015, "value": "72.0",
             "dimensions": {"Location": "URBAN"}},     # dropped in v2
            {"seriesCode": "EG_ACS_ELEC", "geoAreaName": "Rwanda",
             "timePeriodStart": 2015, "value": "12.0",
             "dimensions": {"Location": "RURAL"}},     # dropped in v2
            {"seriesCode": "EG_ACS_ELEC", "geoAreaName": "France",
             "timePeriodStart": 2015, "value": "100.0",
             "dimensions": {"Location": "ALLAREA"}},   # not our country
        ],
    }
    got = parse_unsd_payload(unsd_sample, wanted_geos=set(COUNTRIES))
    assert got == [{"country": "Rwanda", "year": 2015,
                    "series": "EG_ACS_ELEC", "value": 22.8}], got

    wb_sample = [
        {"page": 1, "pages": 1},
        [
            {"countryiso3code": "ETH", "date": "2010", "value": 79.6},
            {"countryiso3code": "ETH", "date": "2011", "value": None},
        ],
    ]
    got = parse_wb_payload(wb_sample)
    assert got == [{"iso3": "ETH", "year": 2010, "value": 79.6}], got

    print("Self-test passed: dimension filter and both parsers behave correctly.")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
