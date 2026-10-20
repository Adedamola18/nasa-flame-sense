"""Export small, real data files for the web app while the full pipeline runs.

A stand-in for scripts 8 (tiles) and 10 (story numbers) until they exist.
Every number the frontend shows comes from these files.

Outputs
  web/src/data/nbac_canada.json   burned area per year, Canada (NBAC)
  web/src/data/scoreboard.json    2021-2024 hold-out metrics per method
  web/src/data/overpass.json      median daytime overpass (local solar hour) per satellite and year
  web/src/data/truth_coverage.json   cell-years and year range per national burned-area record
  web/src/data/story_figures.json    figures for story chapters 1, 2, 4, 6 and 7
  web/public/data/countries.geojson   simplified Natural Earth 1:50m outlines for the globe
  web/public/data/hotspots_2023.json  raw S-NPP detections per 0.25 deg cell, 2023, with region lookups

Run: .venv/bin/python pipeline/web_seed.py
"""

import json
from pathlib import Path

import duckdb
import geopandas as gpd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SRC_OUT = ROOT / "web" / "src" / "data"
PUB_OUT = ROOT / "web" / "public" / "data"
HOTSPOT_YEAR = 2023


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))
    print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size / 1024:.0f} KB)")


def nbac() -> None:
    rows = duckdb.sql(f"""
        select year, sum(burned_ha) as ha
        from '{DATA}/truth/nbac_cells.parquet'
        group by year order by year
    """).fetchall()
    write_json(SRC_OUT / "nbac_canada.json", {
        "source": "NBAC (Canadian Forest Service), summed over 0.25 deg cells",
        "unit": "ha",
        "years": [{"year": y, "ha": round(ha)} for y, ha in rows],
    })


def scoreboard() -> None:
    test_key = "test_2021_2024"
    m = json.loads((DATA / "harmonized" / "ml_metrics.json").read_text())[test_key]
    methods = ["raw_modis", "global_multiplier", "quantile_mapping", "ml"]
    scales = {"cell_month": "Cell-month", "country_iso3_year": "Country-year", "biome_name_year": "Biome-year"}
    out = {
        "test_years": [int(y) for y in test_key.split("_")[1:]],
        "methods": methods,
        "scales": {
            key: {
                "label": label,
                "n": m[key]["ml"]["n"],
                **{meth: {k: m[key][meth][k] for k in ("r", "rmse", "bias_pct", "median_abs_pct_err")} for meth in methods},
            }
            for key, label in scales.items()
        },
    }
    write_json(SRC_OUT / "scoreboard.json", out)


def overpass() -> None:
    q = f"""
        select satellite, year(date) as year, median(solar_hour) as h
        from read_parquet(['{DATA}/grid/global/modis/*.parquet', '{DATA}/grid/global/viirs-snpp/*.parquet'], union_by_name = true)
        where daynight = 'D'
        group by all
        -- Year files spill a few boundary-day rows into the next year; skip those stubs
        having count(*) >= 1000
        order by satellite, year
    """
    series: dict[str, list] = {}
    for sat, year, h in duckdb.sql(q).fetchall():
        name = {"N": "Suomi-NPP"}.get(sat, sat)
        series.setdefault(name, []).append({"year": year, "hour": round(h, 3)})
    write_json(SRC_OUT / "overpass.json", {
        "measure": "median daytime overpass, local solar time, all fire detections worldwide",
        "series": series,
    })


def truth_coverage() -> None:
    sources = [("nbac", "NBAC", "Canada"), ("mtbs", "MTBS", "United States"), ("mapbiomas", "MapBiomas Fire", "Brazil")]
    out = []
    for key, name, country in sources:
        n, y0, y1 = duckdb.sql(f"select count(*), min(year), max(year) from '{DATA}/truth/{key}_cells.parquet'").fetchone()
        # MTBS maps fires a year or more after they burn, so its newest year is incomplete
        usable_last = y1 - 1 if key == "mtbs" else y1
        out.append({"key": key, "name": name, "country": country, "cell_years": n, "first": y0, "last": usable_last})
    write_json(SRC_OUT / "truth_coverage.json", out)


def story_figures() -> None:
    """Figures for the story chapters that are not written yet. Raw S-NPP counts until the harmonised record lands."""
    con = duckdb.connect()
    con.sql(f"""
        create view snpp as
        select g.date, g.n_det, c.lat, c.lon, c.country_iso3, c.biome_num, c.realm, g.row_idx, g.col_idx
        from read_parquet('{DATA}/grid/global/viirs-snpp/*.parquet') g
        join '{DATA}/regions/cells.parquet' c using (row_idx, col_idx)
    """)
    one = lambda q: con.sql(q).fetchone()
    iso = lambda d: d.isoformat()[:10] if d else None

    # Chapter 1: one year of the planet's fires
    year = 2023
    det, cells, countries = one(f"""
        select sum(n_det), count(distinct (row_idx, col_idx)), count(distinct country_iso3)
        from snpp where year(date) = {year} and n_det > 0
    """)
    peak_month, peak_n = one(f"""
        select month(date) m, sum(n_det) n from snpp where year(date) = {year} group by m order by n desc limit 1
    """)
    c1 = {"year": year, "detections": int(det), "perDay": round(det / 365), "cells": cells,
          "countries": countries, "peakMonth": peak_month, "peakShare": round(peak_n / det, 3)}

    # Chapter 2: Terra's morning view against Aqua's afternoon view, same year
    by_sat = dict(con.sql(f"""
        select satellite, sum(n_det) from '{DATA}/grid/global/modis/{year}.parquet'
        where daynight = 'D' and year(date) = {year} group by satellite
    """).fetchall())
    c2 = {"year": year, "terraDay": int(by_sat["Terra"]), "aquaDay": int(by_sat["Aqua"]),
          "aquaToTerra": round(by_sat["Aqua"] / by_sat["Terra"], 2)}

    def featured_fire(fire_year: int, lat: tuple, lon: tuple) -> dict:
        """The largest NBAC fire in a box, plus S-NPP detections in the box while it burned."""
        ha, prov, start, hs, he = one(f"""
            select area_ha, province, agency_start, hotspot_start, hotspot_end
            from '{DATA}/truth/nbac_fires.parquet'
            where year = {fire_year} and lat between {lat[0]} and {lat[1]} and lon between {lon[0]} and {lon[1]}
            order by area_ha desc limit 1
        """)
        window = f"""date between '{iso(hs)}' and '{iso(he)}'
            and lat between {lat[0]} and {lat[1]} and lon between {lon[0]} and {lon[1]}"""
        peak_day, peak_det = one(f"select date, sum(n_det) n from snpp where {window} group by date order by n desc limit 1")
        total, = one(f"select sum(n_det) from snpp where {window}")
        return {"fireHa": round(ha), "province": prov, "agencyStart": iso(start),
                "hotspotStart": iso(hs), "hotspotEnd": iso(he),
                "peakDay": iso(peak_day), "peakDayDetections": int(peak_det), "detections": int(total)}

    # Chapter 4: Horse River fire, Fort McMurray. Box covers the town and the fire's spread east.
    c4 = featured_fire(2016, (56.0, 57.6), (-112.5, -109.5))
    # Chapter 6: Jasper National Park
    c6 = featured_fire(2024, (52.5, 53.2), (-118.5, -117.5))

    # Chapter 7a: Australia's Black Summer (Sep 2019 to Feb 2020) against the same months in earlier S-NPP years.
    # South-east Australia only: the northern savannas burn every spring and would dilute the comparison.
    seasons = con.sql("""
        select case when month(date) >= 9 then year(date) else year(date) - 1 end as season, sum(n_det) n
        from snpp where country_iso3 = 'AUS' and lat between -39.5 and -28 and lon between 140 and 154
          and (month(date) >= 9 or month(date) <= 2)
        group by season having season between 2012 and 2019 order by season
    """).fetchall()
    black = dict(seasons)[2019]
    before = [n for s, n in seasons if s < 2019]
    aus = {"region": "south-east Australia (28–39.5°S, 140–154°E)", "season": "2019–20", "detections": int(black), "baselineFrom": "2012–13", "baselineTo": "2018–19",
           "ratio": round(black / (sum(before) / len(before)), 1)}

    # Chapter 7b: Amazon proxy = Brazilian moist broadleaf forest west of 44°W (excludes the Atlantic Forest)
    months = con.sql("""
        select month(date) m, sum(n_det) n from snpp
        where country_iso3 = 'BRA' and biome_num = 1 and lon < -44 and year(date) between 2012 and 2024
        group by m order by m
    """).fetchall()
    total = sum(n for _, n in months)
    amazon = {"peakMonth": max(months, key=lambda r: r[1])[0],
              "augOctShare": round(sum(n for m, n in months if 8 <= m <= 10) / total, 3),
              "first": 2012, "last": 2024}

    # Chapter 7c: African savannas burn in two seasons, one each side of the equator
    def savanna_peak(where: str) -> int:
        return one(f"""
            select month(date) m, sum(n_det) n from snpp
            where realm = 'Afrotropic' and biome_num = 7 and {where} and year(date) between 2012 and 2024
            group by m order by n desc limit 1
        """)[0]
    africa = {"northPeakMonth": savanna_peak("lat > 0"), "southPeakMonth": savanna_peak("lat < 0")}

    write_json(SRC_OUT / "story_figures.json", {
        "note": "Raw Suomi-NPP VIIRS detections (VNP14IMG) unless named otherwise; NBAC for fire areas and dates.",
        "c1": c1, "c2": c2, "c4": c4, "c6": c6, "c7": {"aus": aus, "amazon": amazon, "africa": africa},
    })


def countries() -> None:
    shp = DATA / "raw" / "regions" / "countries" / "ne_50m_admin_0_countries.shp"
    gdf = gpd.read_file(shp)[["ISO_A3", "NAME", "geometry"]]
    gdf["geometry"] = gdf.geometry.simplify(0.05, preserve_topology=True)
    path = PUB_OUT / "countries.geojson"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(gdf.to_json(drop_id=True, to_wgs84=True))
    print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size / 1024:.0f} KB)")


def hotspots() -> None:
    """Hotspot cells for the globe, with each cell's region details as indexes into lookup tables.

    cells: [lon, lat, n, country, biome, ecoregion, ecozone]; -1 where a cell has none.
    Countries carry an ISO 3166 alpha-2 code so the browser can name them in the reader's language.
    """
    rows = duckdb.sql(f"""
        select round(c.lon, 3), round(c.lat, 3), sum(g.n_det)::int as n,
               c.country_iso3, c.biome_num, c.ecoregion, c.ca_ecozone, c.ca_ecozone_fr
        from '{DATA}/grid/global/viirs-snpp/{HOTSPOT_YEAR}.parquet' g
        join '{DATA}/regions/cells.parquet' c using (row_idx, col_idx)
        where year(g.date) = {HOTSPOT_YEAR}
        group by all having n > 0
        order by n
    """).fetchall()

    # cells.country_iso3 is Natural Earth's ADM0_A3; ISO_A2_EH is -99 for disputed areas
    ne = gpd.read_file(DATA / "raw" / "regions" / "countries" / "ne_50m_admin_0_countries.shp")
    ne_by_a3 = {r.ADM0_A3: (r.ISO_A2_EH if r.ISO_A2_EH != "-99" else None, r.NAME, r.CONTINENT) for r in ne.itertuples()}

    countries, ecoregions, ecozones = [], [], []
    index: dict[tuple, int] = {}

    def idx(table: list, key, entry) -> int:
        if key is None:
            return -1
        k = (id(table), key)
        if k not in index:
            index[k] = len(table)
            table.append(entry)
        return index[k]

    cells = []
    for lon, lat, n, a3, biome, eco, zone_en, zone_fr in rows:
        iso2, name, continent = ne_by_a3.get(a3, (None, a3, None))
        cells.append([
            lon, lat, n,
            idx(countries, a3, {"iso2": iso2, "name": name, "continent": continent}),
            biome if biome is not None else -1,
            idx(ecoregions, eco, eco),
            idx(ecozones, zone_en, {"en": zone_en, "fr": zone_fr}),
        ])

    write_json(PUB_OUT / f"hotspots_{HOTSPOT_YEAR}.json", {
        "source": f"FIRMS VNP14IMG (Suomi-NPP VIIRS 375 m), {HOTSPOT_YEAR}, raw detections per 0.25 deg cell",
        "year": HOTSPOT_YEAR,
        "fields": ["lon", "lat", "n", "country", "biome", "ecoregion", "ecozone"],
        "countries": countries,
        "ecoregions": ecoregions,
        "ecozones": ecozones,
        "cells": cells,
    })


if __name__ == "__main__":
    nbac()
    scoreboard()
    overpass()
    truth_coverage()
    story_figures()
    countries()
    hotspots()
