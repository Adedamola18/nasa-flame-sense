"""Download NASA FIRMS yearly active-fire archives (global, or Canada only for development).

Global:  MODIS C6.1 2000-2024, VIIRS S-NPP 2012-2024, VIIRS NOAA-20 2018-2024 (~10 GB zipped)
Canada:  the same products as per-country CSVs (~2 GB), for building and testing the pipeline

Downloads are resumable: partial files are kept as .part and continued with HTTP Range
requests, and finished files whose size matches the server are skipped. A manifest with
sizes and SHA-256 hashes is written for reproducibility.

Usage:
  python pipeline/01_fetch_firms.py --scope canada
  python pipeline/01_fetch_firms.py --scope global
  python pipeline/01_fetch_firms.py --scope global --products modis --years 2012-2013
"""

import argparse
import hashlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

BASE = "https://firms.modaps.eosdis.nasa.gov/data/country"
RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw" / "firms"
HEADERS = {"User-Agent": "flamesense-pipeline/0.1 (NASA Space Apps 2026)"}

# FIRMS product folder name -> first and last full year available as yearly files
PRODUCTS = {
    "modis": (2000, 2024),        # Terra from Nov 2000, Aqua from Jul 2002
    "viirs-snpp": (2012, 2024),   # Suomi-NPP from Jan 2012
    "viirs-jpss1": (2018, 2024),  # NOAA-20 from Apr 2018
}


def build_jobs(scope: str, products: list[str], years: tuple[int, int] | None) -> list[tuple[str, Path]]:
    jobs = []
    for product in products:
        first, last = PRODUCTS[product]
        if years:
            first, last = max(first, years[0]), min(last, years[1])
        for year in range(first, last + 1):
            if scope == "global":
                name = f"{product}_{year}_all_countries.zip"
                url = f"{BASE}/zips/{name}"
            else:
                name = f"{product}_{year}_Canada.csv"
                url = f"{BASE}/{product}/{year}/{name}"
            jobs.append((url, RAW_DIR / scope / product / name))
    return jobs


def remote_size(client: httpx.Client, url: str) -> int | None:
    response = client.head(url)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return int(response.headers.get("content-length", 0))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, dest: Path, retries: int = 5) -> dict:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    with httpx.Client(headers=HEADERS, timeout=httpx.Timeout(60, read=300), follow_redirects=True) as client:
        size = remote_size(client, url)
        if size is None:
            return {"url": url, "status": "missing"}
        if dest.exists() and dest.stat().st_size == size:
            return {"url": url, "file": str(dest), "bytes": size, "status": "cached"}

        for attempt in range(1, retries + 1):
            have = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={have}-"} if have else {}
            try:
                with client.stream("GET", url, headers=headers) as response:
                    if have and response.status_code != 206:
                        have = 0  # server ignored the range; start over
                    response.raise_for_status()
                    with part.open("ab" if have else "wb") as f:
                        for chunk in response.iter_bytes(1 << 20):
                            f.write(chunk)
                if part.stat().st_size == size:
                    part.rename(dest)
                    return {"url": url, "file": str(dest), "bytes": size, "status": "downloaded"}
            except (httpx.HTTPError, OSError) as error:
                print(f"  retry {attempt}/{retries} {dest.name}: {error}", file=sys.stderr)
            time.sleep(min(60, 5 * attempt))
        return {"url": url, "status": "failed"}


def parse_years(value: str) -> tuple[int, int]:
    start, _, end = value.partition("-")
    return int(start), int(end or start)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scope", choices=["global", "canada"], required=True)
    parser.add_argument("--products", nargs="+", choices=list(PRODUCTS), default=list(PRODUCTS))
    parser.add_argument("--years", type=parse_years, help="e.g. 2012-2024 or 2023")
    parser.add_argument("--workers", type=int, default=3, help="parallel downloads (be polite to FIRMS)")
    args = parser.parse_args()

    jobs = build_jobs(args.scope, args.products, args.years)
    print(f"{len(jobs)} files to check ({args.scope}) -> {RAW_DIR / args.scope}")

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(download, url, dest): dest for url, dest in jobs}
        for done, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            results.append(result)
            mb = result.get("bytes", 0) / 1_048_576
            print(f"[{done}/{len(jobs)}] {result['status']:<10} {futures[future].name} {mb:,.0f} MB", flush=True)

    for result in results:
        if result["status"] in ("downloaded", "cached"):
            result["sha256"] = sha256(Path(result["file"]))
            result["file"] = str(Path(result["file"]).relative_to(RAW_DIR.parent.parent))

    manifest = RAW_DIR / f"manifest_{args.scope}.json"
    manifest.write_text(json.dumps(sorted(results, key=lambda r: r["url"]), indent=2))

    failed = [r for r in results if r["status"] == "failed"]
    total_gb = sum(r.get("bytes", 0) for r in results) / 1_073_741_824
    print(f"Done: {total_gb:.2f} GB on disk, {len(failed)} failed. Manifest: {manifest.name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
