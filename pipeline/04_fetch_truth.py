"""Download independent burned-area records and put them on the same 0.25-degree grid as FIRMS.

These are the validation ground truth: mapped from Landsat/Sentinel-2 imagery and agency
fire perimeters, independent of MODIS and VIIRS active-fire detections.

  NBAC       Canada  National Burned Area Composite (NRCan, CWFIS)     yearly perimeters
  MTBS       USA     Monitoring Trends in Burn Severity (USGS/USFS)    perimeters, large fires
  MapBiomas  Brazil  MapBiomas Fire, Collection 5.1                    yearly 30 m rasters

Outputs (data/truth/):
  {source}_cells.parquet   year, row_idx, col_idx, burned_ha
  nbac_fires.parquet       one row per Canadian fire: dates, cause, province, area, centroid
  mtbs_fires.parquet       one row per US fire: ignition date, type, area, centroid

Usage:
  python pipeline/04_fetch_truth.py                      # all sources, 2001-2025
  python pipeline/04_fetch_truth.py --sources nbac --years 2023
"""

import argparse
import sys
import zipfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import geopandas as gpd
import httpx
import numpy as np
import pandas as pd
import rasterio
import rasterio.features
import rasterio.windows

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "truth"
OUT = ROOT / "data" / "truth"
CELL = 0.25
EQUAL_AREA = "EPSG:6933"  # WGS 84 / NSIDC EASE-Grid 2.0 Global, equal-area, for hectares
HEADERS = {"User-Agent": "flamesense-pipeline/0.1 (NASA Space Apps 2026)"}

NBAC_URL = "https://cwfis.cfs.nrcan.gc.ca/downloads/nbac/NBAC_{year}_20260513.zip"
MTBS_URL = ("https://edcintl.cr.usgs.gov/downloads/sciweb1/shared/MTBS_Fire/data/composite_data/"
            "burned_area_extent_shapefile/mtbs_perimeter_data.zip")
MAPBIOMAS_URL = ("https://storage.googleapis.com/mapbiomas-public/initiatives/brazil/fire/collection_05_1/"
                 "annual_burned/annual_burned-brazil_burned_area_{year}.tif")


def download(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    with httpx.stream("GET", url, headers=HEADERS, timeout=httpx.Timeout(60, read=600), follow_redirects=True) as r:
        r.raise_for_status()
        with part.open("wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    part.rename(dest)
    return dest


def unzip_shapefile(archive: Path) -> Path:
    folder = archive.with_suffix("")
    if not any(folder.rglob("*.shp")):
        with zipfile.ZipFile(archive) as z:
            z.extractall(folder)
    return next(folder.rglob("*.shp"))


def cell_area_ha(row_idx: np.ndarray) -> np.ndarray:
    """Area of a 0.25-degree cell in hectares, from its latitude band (spherical approximation)."""
    radius_m = 6_371_007.2
    lat1 = np.radians(row_idx * CELL - 90)
    lat2 = np.radians((row_idx + 1) * CELL - 90)
    return radius_m**2 * np.radians(CELL) * (np.sin(lat2) - np.sin(lat1)) / 10_000


SUB = 50  # rasterise perimeters at CELL/SUB = 0.005 degrees (~500 m), then sum up to cells


def polygons_to_cells(fires: gpd.GeoDataFrame) -> pd.DataFrame:
    """Rasterise fire perimeters on a fine grid snapped to cell edges and sum burned hectares per cell.

    Overlapping perimeters within a year count once (a pixel is burned or not), matching how
    burned area is reported. Polygon overlay was exact but took minutes per year for 2023.
    """
    fires = fires.to_crs("EPSG:4326")[["year", "geometry"]].copy()
    fires = fires[fires.geometry.notna() & ~fires.geometry.is_empty]
    frames = []
    for year, group in fires.groupby("year"):
        xmin, ymin, xmax, ymax = group.total_bounds
        left = np.floor((xmin + 180) / CELL) * CELL - 180
        bottom = np.floor((ymin + 90) / CELL) * CELL - 90
        n_cols = int(np.ceil((xmax - left) / CELL))
        n_rows = int(np.ceil((ymax - bottom) / CELL))
        top = bottom + n_rows * CELL
        transform = rasterio.transform.from_origin(left, top, CELL / SUB, CELL / SUB)
        mask = rasterio.features.rasterize(
            ((geom, 1) for geom in group.geometry), out_shape=(n_rows * SUB, n_cols * SUB),
            transform=transform, fill=0, dtype="uint8",
        )
        fraction = mask.reshape(n_rows, SUB, n_cols, SUB).mean(axis=(1, 3))
        rr, cc = np.nonzero(fraction)
        row_idx = (np.round((top - (rr + 1) * CELL + 90) / CELL)).astype("int16")
        col_idx = (np.round((left + cc * CELL + 180) / CELL)).astype("int16")
        frames.append(pd.DataFrame({
            "year": int(year), "row_idx": row_idx, "col_idx": col_idx,
            "burned_ha": fraction[rr, cc] * cell_area_ha(row_idx),
        }))
    return pd.concat(frames, ignore_index=True)


def fire_table(fires: gpd.GeoDataFrame, columns: dict[str, str]) -> pd.DataFrame:
    centroids = fires.to_crs(EQUAL_AREA).centroid.to_crs("EPSG:4326")
    table = fires.rename(columns=columns)[list(columns.values())].copy()
    table["lat"], table["lon"] = centroids.y.values, centroids.x.values
    return pd.DataFrame(table)


def run_nbac(years: range) -> None:
    frames = []
    for year in years:
        shp = unzip_shapefile(download(NBAC_URL.format(year=year), RAW / "nbac" / f"NBAC_{year}_20260513.zip"))
        fires = gpd.read_file(shp)
        fires["year"] = year
        frames.append(fires)
        print(f"  NBAC {year}: {len(fires):,} fires, {fires['ADJ_HA'].sum() / 1e6:.2f} Mha", flush=True)
    fires = pd.concat(frames, ignore_index=True)
    fires = gpd.GeoDataFrame(fires, geometry="geometry", crs=frames[0].crs)

    polygons_to_cells(fires).to_parquet(OUT / "nbac_cells.parquet", index=False)
    fire_table(fires, {
        "year": "year", "GID": "fire_id", "AG_SDATE": "agency_start", "AG_EDATE": "agency_end",
        "HS_SDATE": "hotspot_start", "HS_EDATE": "hotspot_end", "FIRECAUS": "cause",
        "ADMIN_NAME": "province", "PRESCRIBED": "prescribed", "ADJ_HA": "area_ha",
    }).to_parquet(OUT / "nbac_fires.parquet", index=False)


def run_mtbs(years: range) -> None:
    shp = unzip_shapefile(download(MTBS_URL, RAW / "mtbs" / "mtbs_perimeter_data.zip"))
    fires = gpd.read_file(shp)
    # MTBS has published these fields in both CamelCase (Ig_Date) and lowercase (ig_date).
    fires.columns = [c if c == "geometry" else c.lower() for c in fires.columns]
    fires["ignition"] = pd.to_datetime(fires["ig_date"], errors="coerce")
    fires["year"] = fires["ignition"].dt.year
    fires = fires[fires["year"].between(years.start, years.stop - 1)].copy()
    fires["area_ha"] = fires["burnbndac"] * 0.404686
    for year, group in fires.groupby("year"):
        print(f"  MTBS {year}: {len(group):,} fires, {group['area_ha'].sum() / 1e6:.2f} Mha", flush=True)

    polygons_to_cells(fires).to_parquet(OUT / "mtbs_cells.parquet", index=False)
    fire_table(fires, {
        "year": "year", "event_id": "fire_id", "ignition": "ignition", "incid_type": "incident_type",
        "incid_name": "name", "area_ha": "area_ha",
    }).to_parquet(OUT / "mtbs_fires.parquet", index=False)


def mapbiomas_year(year: int) -> pd.DataFrame:
    """Exact burned hectares per cell from one 30 m MapBiomas raster (0 = unburned, 1 = burned).

    GDAL's average warp undercounts badly at this reduction ratio (~928 x 928 pixels per cell),
    so pixels are counted directly: read row strips, sum burned pixels per cell column with
    np.add.reduceat, weight each row by its true pixel area, then sum rows into cell rows.
    """
    tif = download(MAPBIOMAS_URL.format(year=year), RAW / "mapbiomas" / f"annual_burned-brazil_burned_area_{year}.tif")
    radius_m = 6_371_007.2
    with rasterio.open(tif) as src:
        res_x, res_y = src.res
        left, top = src.transform.c, src.transform.f
        col_cell = np.floor((left + (np.arange(src.width) + 0.5) * res_x + 180) / CELL).astype(np.int32)
        starts = np.r_[0, np.flatnonzero(np.diff(col_cell)) + 1]
        cell_cols = col_cell[starts]
        totals: dict[int, np.ndarray] = {}
        strip = 1024
        for r0 in range(0, src.height, strip):
            n = min(strip, src.height - r0)
            burned = src.read(1, window=rasterio.windows.Window(0, r0, src.width, n)) == 1
            if not burned.any():
                continue
            per_cell_col = np.add.reduceat(burned, starts, axis=1, dtype=np.uint32)
            lat_top = top - (r0 + np.arange(n)) * res_y
            lat_bot = lat_top - res_y
            px_ha = radius_m**2 * np.radians(res_x) * (np.sin(np.radians(lat_top)) - np.sin(np.radians(lat_bot))) / 10_000
            row_cell = np.floor(((lat_top + lat_bot) / 2 + 90) / CELL).astype(np.int32)
            weighted = per_cell_col * px_ha[:, None]
            for rc in np.unique(row_cell):
                totals[rc] = totals.get(rc, 0) + weighted[row_cell == rc].sum(axis=0)
    rows, cols, values = [], [], []
    for rc, vec in totals.items():
        nz = np.flatnonzero(vec)
        rows.append(np.full(nz.size, rc)); cols.append(cell_cols[nz]); values.append(vec[nz])
    frame = pd.DataFrame({"year": year, "row_idx": np.concatenate(rows).astype("int16"),
                          "col_idx": np.concatenate(cols).astype("int16"), "burned_ha": np.concatenate(values)})
    print(f"  MapBiomas {year}: {frame.burned_ha.sum() / 1e6:.2f} Mha", flush=True)
    return frame


def run_mapbiomas(years: range, workers: int = 4) -> None:
    with ProcessPoolExecutor(max_workers=workers) as pool:
        frames = list(pool.map(mapbiomas_year, years))
    pd.concat(frames, ignore_index=True).to_parquet(OUT / "mapbiomas_cells.parquet", index=False)


def parse_years(value: str) -> range:
    start, _, end = value.partition("-")
    return range(int(start), int(end or start) + 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sources", nargs="+", choices=["nbac", "mtbs", "mapbiomas"], default=["nbac", "mtbs", "mapbiomas"])
    parser.add_argument("--years", type=parse_years, default=range(2001, 2026))
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    runners = {"nbac": run_nbac, "mtbs": run_mtbs, "mapbiomas": run_mapbiomas}
    for source in args.sources:
        print(f"{source.upper()} {args.years.start}-{args.years.stop - 1}")
        runners[source](args.years)
    return 0


if __name__ == "__main__":
    sys.exit(main())
