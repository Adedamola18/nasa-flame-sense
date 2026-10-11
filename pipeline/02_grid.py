"""Aggregate FIRMS active-fire detections to a 0.25-degree daily grid, per satellite and overpass.

Input:   data/raw/firms/{scope}/{product}/*  (Canada CSVs, or global yearly zips)
Output:  data/grid/{scope}/{product}/{year}.parquet

One output row = one grid cell, one local solar day, one satellite, day or night overpass.

Filtering (applied to the "n_det" and "frp_*" columns):
  - type == 0 (presumed vegetation fire; drops volcanoes, static industrial sources, offshore)
  - MODIS confidence >= 30; VIIRS confidence nominal or high
Unfiltered vegetation-fire counts are kept as "n_det_all" for sensitivity checks.

Days are local solar days (UTC time shifted by longitude/15 hours), so a single overpass
is never split across two dates, and Aqua (~13:30) pairs cleanly with Suomi-NPP (~13:30).

Usage:
  python pipeline/02_grid.py --scope canada
  python pipeline/02_grid.py --scope global --products modis --years 2012-2013
"""

import argparse
import sys
import tempfile
import zipfile
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw" / "firms"
GRID_DIR = ROOT / "data" / "grid"
CELL = 0.25
PRODUCTS = ("modis", "viirs-snpp", "viirs-jpss1")

MODIS_CONFIDENT = "confidence >= 30"
VIIRS_CONFIDENT = "confidence IN ('n', 'h')"


def grid_sql(source: str, product: str) -> str:
    is_modis = product == "modis"
    confident = MODIS_CONFIDENT if is_modis else VIIRS_CONFIDENT
    # VIIRS confidence is a letter; give it a numeric scale so both sensors share a column.
    conf_score = "confidence::DOUBLE" if is_modis else "CASE confidence WHEN 'l' THEN 0 WHEN 'n' THEN 50 ELSE 100 END"
    return f"""
    WITH d AS (
        SELECT
            latitude, longitude, frp, scan, track, satellite, daynight, confidence,
            {conf_score} AS conf_score,
            acq_date::TIMESTAMP
              + to_minutes((lpad(acq_time::VARCHAR, 4, '0')[1:2])::INT * 60 + (lpad(acq_time::VARCHAR, 4, '0')[3:4])::INT)
              + to_minutes(round(longitude * 4)::INT) AS solar_ts
        FROM read_csv({source}, union_by_name = true, ignore_errors = true)
        WHERE type = 0
    )
    SELECT
        solar_ts::DATE                                   AS date,
        floor((latitude + 90) / {CELL})::SMALLINT        AS row_idx,
        floor((longitude + 180) / {CELL})::SMALLINT      AS col_idx,
        satellite,
        daynight,
        count(*) FILTER (WHERE {confident})              AS n_det,
        count(*)                                         AS n_det_all,
        coalesce(sum(frp) FILTER (WHERE {confident}), 0) AS frp_sum,
        coalesce(max(frp) FILTER (WHERE {confident}), 0) AS frp_max,
        avg(scan)                                        AS scan_mean,
        avg(track)                                       AS track_mean,
        avg(conf_score)                                  AS conf_mean,
        avg(hour(solar_ts) + minute(solar_ts) / 60.0)    AS solar_hour
    FROM d
    GROUP BY ALL
    """


def year_inputs(scope: str, product: str) -> dict[int, Path]:
    folder = RAW_DIR / scope / product
    pattern = "*_all_countries.zip" if scope == "global" else "*_Canada.csv"
    return {int(p.name.split("_")[1]): p for p in sorted(folder.glob(pattern))}


def grid_year(con: duckdb.DuckDBPyConnection, path: Path, product: str, out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".zip":
        # Global archives hold one CSV per country; extract to a temp dir, grid, then discard.
        with tempfile.TemporaryDirectory() as tmp:
            with zipfile.ZipFile(path) as archive:
                archive.extractall(tmp)
            csvs = sorted(str(p) for p in Path(tmp).rglob("*.csv"))
            source = "[" + ", ".join(f"'{c}'" for c in csvs) + "]"
            con.sql(f"COPY ({grid_sql(source, product)}) TO '{out}' (FORMAT parquet, COMPRESSION zstd)")
    else:
        con.sql(f"COPY ({grid_sql(repr(str(path)), product)}) TO '{out}' (FORMAT parquet, COMPRESSION zstd)")
    return con.sql(f"SELECT count(*) FROM '{out}'").fetchone()[0]


def parse_years(value: str) -> tuple[int, int]:
    start, _, end = value.partition("-")
    return int(start), int(end or start)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scope", choices=["global", "canada"], required=True)
    parser.add_argument("--products", nargs="+", choices=PRODUCTS, default=list(PRODUCTS))
    parser.add_argument("--years", type=parse_years)
    parser.add_argument("--force", action="store_true", help="rebuild years that already have output")
    args = parser.parse_args()

    con = duckdb.connect()
    con.sql("SET preserve_insertion_order = false")
    for product in args.products:
        for year, path in year_inputs(args.scope, product).items():
            if args.years and not (args.years[0] <= year <= args.years[1]):
                continue
            out = GRID_DIR / args.scope / product / f"{year}.parquet"
            if out.exists() and not args.force:
                print(f"skip   {product} {year} (exists)")
                continue
            rows = grid_year(con, path, product, out)
            print(f"grid   {product} {year}: {rows:,} cell-day rows -> {out.relative_to(ROOT)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
