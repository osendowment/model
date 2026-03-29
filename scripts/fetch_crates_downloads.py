"""
Fetch Rust crate download data from crates.io and produce
data/crates/top-package-downloads.csv with yearly download counts (2021-2025).

Data sources:
  DB dump : https://static.crates.io/db-dump.tar.gz
            Provides crate name mapping (crates.csv, versions.csv).
            Note: version_downloads.csv in the dump only covers the last ~90 days,
            so it cannot be used for full yearly historical data.
  Archive : https://static.crates.io/archive/version-downloads/{date}.csv
            Daily per-version download counts; used to build per-year totals.
            Downloaded files are cached in .cache/crates/.

Run:
    uv run scripts/fetch_crates_downloads.py
"""

import asyncio
import csv
import os
import tarfile

import httpx
import pandas as pd
import requests
from tqdm import tqdm

PATH = "data/crates"
CACHE = ".cache/crates"
os.makedirs(PATH, exist_ok=True)
os.makedirs(CACHE, exist_ok=True)

DUMP_URL = "https://static.crates.io/db-dump.tar.gz"
DUMP_PATH = f"{PATH}/db-dump.tar.gz"
ARCHIVE_INDEX = "https://static.crates.io/archive/version-downloads/index.json"
ARCHIVE_BASE = "https://static.crates.io/archive/version-downloads"

YEARS = range(2021, 2026)
MIN_AVG_DOWNLOADS = 1_000_000
CONCURRENCY = 40


# ── Step 1: Download dump ──────────────────────────────────────────────────────

if os.path.exists(DUMP_PATH):
    print(f"Dump already exists: {DUMP_PATH} — skipping download")
else:
    print(f"Downloading DB dump from {DUMP_URL} ...")
    r = requests.get(DUMP_URL, stream=True)
    size = int(r.headers.get("content-length", 0))
    with open(DUMP_PATH, "wb") as f, tqdm(total=size, unit="iB", unit_scale=True) as pbar:
        for chunk in r.iter_content(chunk_size=65536):
            pbar.update(f.write(chunk))
    print(f"Saved to {DUMP_PATH}")


# ── Step 2: Extract crates.csv + versions.csv from dump ───────────────────────

EXTRACT_TARGETS = ["crates.csv", "versions.csv", "dependencies.csv"]
missing = [t for t in EXTRACT_TARGETS if not os.path.exists(f"{PATH}/{t}")]
if missing:
    print(f"\nExtracting {missing} from dump ...")
    with tarfile.open(DUMP_PATH, "r:gz") as tar:
        for member in tar.getmembers():
            if "/data/" not in member.name:
                continue
            fname = member.name.split("/data/")[-1]
            if fname in missing:
                member.name = fname
                tar.extract(member, PATH)
                print(f"  Extracted {fname} → {PATH}/{fname}")
else:
    print("crates.csv + versions.csv already extracted — skipping")


# ── Step 3: Download per-year version download totals from archive ─────────────

async def fetch_daily(client, sem, url):
    async with sem:
        for attempt in range(3):
            try:
                r = await client.get(url, timeout=60)
                if r.status_code >= 500:
                    if attempt < 2:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    print(f"  skip {url.split('/')[-1]} (HTTP {r.status_code})")
                    return {}
                r.raise_for_status()
                result = {}
                for line in r.text.splitlines()[1:]:
                    if "," in line:
                        vid, dl = line.split(",", 1)
                        result[int(vid)] = int(dl)
                return result
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                else:
                    print(f"  skip {url.split('/')[-1]} ({e})")
                    return {}
    return {}


async def download_year(year):
    cache_path = f"{CACHE}/downloads_{year}.csv"
    if os.path.exists(cache_path):
        print(f"  {year}: using cached {cache_path}")
        df = pd.read_csv(cache_path)
        return df.set_index("version_id")["downloads"].to_dict()

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(ARCHIVE_INDEX)
        index = r.json()

    year_urls = [f"{ARCHIVE_BASE}/{e['name']}" for e in index if e["name"].startswith(str(year))]
    total_mb = sum(e["size"] for e in index if e["name"].startswith(str(year))) / 1024**2
    print(f"  {year}: downloading {len(year_urls)} files ({total_mb:.0f} MB) ...")

    totals = {}
    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(timeout=60, http2=True) as client:
        tasks = [fetch_daily(client, sem, url) for url in year_urls]
        for daily in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc=str(year)):
            for vid, dl in (await daily).items():
                totals[vid] = totals.get(vid, 0) + dl

    pd.DataFrame(list(totals.items()), columns=["version_id", "downloads"]).to_csv(cache_path, index=False)
    print(f"  {year}: {len(totals):,} versions → cached to {cache_path}")
    return totals


print("\nFetching per-year downloads ...")
year_data = {}
for year in YEARS:
    year_data[year] = asyncio.run(download_year(year))


# ── Step 4: Aggregate version-level → crate-level ─────────────────────────────

print("\nLoading crate name mappings ...")
csv.field_size_limit(10 * 1024 * 1024)
crates = pd.read_csv(f"{PATH}/crates.csv", usecols=["id", "name"]).rename(columns={"id": "crate_id"})
versions = pd.read_csv(f"{PATH}/versions.csv", usecols=["id", "crate_id"]).rename(columns={"id": "version_id"})

print("Building crate-level yearly download table ...")
frames = []
for year in YEARS:
    df = pd.DataFrame(list(year_data[year].items()), columns=["version_id", "downloads"])
    df["year"] = year
    frames.append(df)

all_dl = pd.concat(frames)
all_dl = all_dl.merge(versions, on="version_id", how="left").dropna(subset=["crate_id"])
all_dl["crate_id"] = all_dl["crate_id"].astype(int)

pivoted = (
    all_dl.groupby(["crate_id", "year"])["downloads"]
    .sum()
    .reset_index()
    .pivot_table(index="crate_id", columns="year", values="downloads", aggfunc="sum", fill_value=0)
)
pivoted.columns = [str(int(c)) for c in pivoted.columns]

year_cols = [str(y) for y in YEARS]
for col in year_cols:
    if col not in pivoted.columns:
        pivoted[col] = 0
pivoted = pivoted[year_cols]

pivoted["avg_downloads"] = pivoted[year_cols].mean(axis=1).astype(int)
pivoted = pivoted[pivoted["avg_downloads"] >= MIN_AVG_DOWNLOADS]
pivoted = pivoted.reset_index().merge(crates, on="crate_id", how="left").dropna(subset=["name"])
pivoted = pivoted.sort_values("avg_downloads", ascending=False)
pivoted = pivoted[["name", "avg_downloads"] + year_cols].rename(columns={"name": "package"})

out = f"{PATH}/top-package-downloads.csv"
pivoted.to_csv(out, index=False, quoting=csv.QUOTE_ALL)
print(f"\nWritten {len(pivoted):,} packages (avg ≥ {MIN_AVG_DOWNLOADS:,}) to {out}")
print(pivoted.head(10).to_string(index=False))
