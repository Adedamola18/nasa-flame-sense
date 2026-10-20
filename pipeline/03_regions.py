"""Tag every 0.25-degree grid cell that ever had a fire with its biome, ecoregion, country and Canadian ecozone.

Sources (downloaded to data/raw/regions/ on first run):
  - RESOLVE Ecoregions 2017 (Dinerstein et al. 2017): 846 ecoregions in 14 biomes
  - Natural Earth 1:50m admin-0 countries
  - Terrestrial Ecozones of Canada (Agriculture and Agri-Food Canada, National Ecological Framework)

Each cell is matched by its centre point. Coastal cells whose centre falls in the sea are
matched to the nearest polygon within ~30 km, so shoreline fires keep a biome and country.

Output: data/regions/cells.parquet
  row_idx, col_idx, lat, lon, biome_num, biome_name, ecoregion, realm,
  country_iso3, country_name, ca_ecozone, ca_ecozone_fr
"""

import sys
import warnings
import zipfile
from pathlib import Path

import duckdb
import geopandas as gpd
import httpx
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "regions"
GRID = ROOT / "data" / "grid"
OUT = ROOT / "data" / "regions" / "cells.parquet"
CELL = 0.25
NEAREST_MAX_DEG = 0.3  # ~30 km at mid latitudes

# The nearest-polygon fallback deliberately measures distance in degrees (a coarse coastal
# snap, not a precise distance), so geopandas' geographic-CRS warning does not apply here.
warnings.filterwarnings("ignore", message="Geometry is in a geographic CRS")
HEADERS = {"User-Agent": "flamesense-pipeline/0.1 (NASA Space Apps 2026)"}

SOURCES = {
    "ecoregions": "https://storage.googleapis.com/teow2016/Ecoregions2017.zip",
    "countries": "https://naciscdn.org/naturalearth/50m/cultural/ne_50m_admin_0_countries.zip",
    "ca_ecozones": "https://sis.agr.gc.ca/cansis/nsdb/ecostrat/zone/ecozone_shp.zip",
}


def fetch(name: str, url: str) -> Path:
    folder = RAW / name
    if any(folder.rglob("*.shp")):
        return next(folder.rglob("*.shp"))
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / f"{name}.zip"
    with httpx.stream("GET", url, headers=HEADERS, timeout=300, follow_redirects=True) as response:
        response.raise_for_status()
        with archive.open("wb") as f:
            for chunk in response.iter_bytes(1 << 20):
                f.write(chunk)
    with zipfile.ZipFile(archive) as z:
        z.extractall(folder)
    archive.unlink()
    return next(folder.rglob("*.shp"))


def fire_cells() -> gpd.GeoDataFrame:
    cells = duckdb.sql(f"""
        SELECT DISTINCT row_idx, col_idx
        FROM read_parquet('{GRID}/*/*/*.parquet')  -- whichever scopes were gridded (global already covers Canada)
    """).df()
    cells["lat"] = (cells.row_idx + 0.5) * CELL - 90
    cells["lon"] = (cells.col_idx + 0.5) * CELL - 180
    return gpd.GeoDataFrame(cells, geometry=gpd.points_from_xy(cells.lon, cells.lat), crs="EPSG:4326")


def join(points: gpd.GeoDataFrame, polygons: gpd.GeoDataFrame, columns: list[str]) -> pd.DataFrame:
    """Point-in-polygon, then nearest polygon for points left unmatched (coastal cells)."""
    polygons = polygons[columns + ["geometry"]]
    hit = gpd.sjoin(points, polygons, how="left", predicate="within").drop(columns="index_right")
    hit = hit[~hit.index.duplicated(keep="first")]
    missing = hit[columns[0]].isna()
    if missing.any():
        near = gpd.sjoin_nearest(points[missing], polygons, how="left", max_distance=NEAREST_MAX_DEG)
        near = near[~near.index.duplicated(keep="first")]
        hit.loc[missing, columns] = near[columns]
    return hit[columns]


def main() -> int:
    shapes = {name: fetch(name, url) for name, url in SOURCES.items()}
    points = fire_cells()
    print(f"{len(points):,} grid cells with at least one fire detection")

    eco = gpd.read_file(shapes["ecoregions"]).to_crs("EPSG:4326")
    # "Rock and Ice" carries biome name "N/A"; drop it so glacier-edge cells take the nearest real biome.
    eco = eco[(eco["BIOME_NUM"].fillna(0) > 0) & (eco["BIOME_NAME"] != "N/A")]
    eco = eco.rename(columns={"BIOME_NUM": "biome_num", "BIOME_NAME": "biome_name", "ECO_NAME": "ecoregion", "REALM": "realm"})

    countries = gpd.read_file(shapes["countries"]).to_crs("EPSG:4326")
    countries = countries.rename(columns={"ADM0_A3": "country_iso3", "NAME": "country_name"})

    zones = gpd.read_file(shapes["ca_ecozones"]).to_crs("EPSG:4326")
    zones = zones.rename(columns={"ZONE_NAME": "ca_ecozone", "ZONE_NOM": "ca_ecozone_fr"})
    zones["ca_ecozone"] = zones["ca_ecozone"].str.title()  # source has "Boreal PLain"

    # Point-in-polygon on the RESOLVE dataset is the slow step; simplify slightly first.
    eco["geometry"] = eco.geometry.simplify(0.01, preserve_topology=True)
    result = points[["row_idx", "col_idx", "lat", "lon"]].copy()
    result = result.join(join(points, eco, ["biome_num", "biome_name", "ecoregion", "realm"]))
    result = result.join(join(points, countries, ["country_iso3", "country_name"]))
    in_canada = result.country_iso3 == "CAN"
    result[["ca_ecozone", "ca_ecozone_fr"]] = None
    result.loc[in_canada, ["ca_ecozone", "ca_ecozone_fr"]] = join(points[in_canada], zones, ["ca_ecozone", "ca_ecozone_fr"])

    # Cells beyond the coastal snap distance from any real biome sit on ice or bare rock
    # (Greenland, Antarctica, high mountains). Label them rather than leave them blank.
    no_biome = result["biome_num"].isna()
    result.loc[no_biome, ["biome_num", "biome_name", "ecoregion"]] = [99, "Rock & Ice", "Rock and Ice"]
    result["biome_num"] = result["biome_num"].astype("Int16")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    result.to_parquet(OUT, index=False)

    print(f"Saved {OUT.relative_to(ROOT)}")
    print(f"  no biome:   {result.biome_num.isna().sum():,} cells")
    print(f"  no country: {result.country_iso3.isna().sum():,} cells")
    print(f"  Canada:     {in_canada.sum():,} cells, {result.ca_ecozone.notna().sum():,} with an ecozone")
    print("  cells per biome:")
    for name, n in result.biome_name.value_counts().items():
        print(f"    {n:>7,}  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
