"""Statistical baseline: quantile-map MODIS fire counts into VIIRS-equivalent counts.

Method (per biome x season group, fitted on the Aqua / Suomi-NPP overlap):
  1. Aggregate both sensors to 0.25-degree cell-months (confident vegetation-fire detections,
     day + night passes; Aqua and Suomi-NPP both cross at ~13:30 and ~01:30).
  2. For cell-months where MODIS saw fire, map each MODIS count to the VIIRS count at the same
     percentile ("located" VIIRS-equivalent). Counts beyond the training range scale by the
     ratio at the top of the distribution.
  3. Fires MODIS missed entirely (cell-months with MODIS = 0, VIIRS > 0) cannot be placed on a
     map before 2012, so each group's missed share R is learned from the overlap and applied
     to totals: total = located x (1 + R).
  4. Groups with too little training data fall back to the whole biome, then to global.

Terra-only months (before Aqua's July 2002 launch) use a separate Terra -> VIIRS mapping
fitted on 2012-2019, before Terra's orbit drift became severe.

Evaluation: fit on 2012-2020, apply to 2021-2024 MODIS, compare with actual VIIRS. References:
raw MODIS and a single global multiplier (VIIRS = k x MODIS).

Outputs (data/harmonized/):
  baseline_cellmonth.parquet   year, month, row_idx, col_idx, sensor, modis_n,
                               viirs_eq_located, viirs_eq_total, viirs_obs
  baseline_mapping.parquet     the fitted lookup tables (for transparency)
  baseline_metrics.json        hold-out scores at cell-month and region-year scale
"""

import json
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
GRID = ROOT / "data" / "grid" / "global"
REGIONS = ROOT / "data" / "regions" / "cells.parquet"
OUT = ROOT / "data" / "harmonized"

TRAIN = (2012, 2020)
TEST = (2021, 2024)
TERRA_TRAIN = (2012, 2019)
MIN_SAMPLES = 300
SEASONS = ["DJF", "MAM", "JJA", "SON"]


def load_cell_months() -> pd.DataFrame:
    """One row per cell-month with any confident detection from Aqua, Terra or Suomi-NPP."""
    df = duckdb.sql(f"""
        WITH m AS (
            SELECT row_idx, col_idx, year(date) AS year, month(date) AS month,
                   sum(n_det) FILTER (WHERE satellite = 'Aqua')  AS aqua,
                   sum(n_det) FILTER (WHERE satellite = 'Terra') AS terra
            FROM read_parquet('{GRID}/modis/*.parquet')
            GROUP BY ALL
        ),
        v AS (
            SELECT row_idx, col_idx, year(date) AS year, month(date) AS month, sum(n_det) AS snpp
            FROM read_parquet('{GRID}/viirs-snpp/*.parquet')
            GROUP BY ALL
        )
        SELECT row_idx, col_idx, year, month,
               coalesce(aqua, 0)::INT AS aqua, coalesce(terra, 0)::INT AS terra,
               snpp::INT AS snpp
        FROM m FULL OUTER JOIN v USING (row_idx, col_idx, year, month)
        WHERE year BETWEEN 2001 AND 2024
    """).df()
    df = df[(df.aqua > 0) | (df.terra > 0) | (df.snpp.fillna(0) > 0)].copy()
    # From 2012 S-NPP was flying: a cell-month with no S-NPP row means it saw no fire (0), not
    # "unknown". Leaving it NaN would drop ~6% of seen cell-months from the fit and bias QM upward.
    s_npp_era = df.year >= 2012
    df.loc[s_npp_era, "snpp"] = df.loc[s_npp_era, "snpp"].fillna(0)

    regions = pd.read_parquet(REGIONS)[["row_idx", "col_idx", "lat", "biome_num", "biome_name", "country_iso3", "ca_ecozone"]]
    df = df.merge(regions, on=["row_idx", "col_idx"], how="left")
    # Season by hemisphere: southern-hemisphere months shift by six months so "JJA" always means summer.
    season = (df.month % 12) // 3
    season = np.where(df.lat < 0, (season + 2) % 4, season)
    df["season"] = pd.Categorical.from_codes(season, SEASONS)
    return df


def fit_group(modis: np.ndarray, viirs: np.ndarray) -> dict:
    """Quantile-mapping table for one group, plus the missed-fire share R."""
    seen = modis > 0
    m, v = modis[seen], viirs[seen]
    values, counts = np.unique(m, return_counts=True)
    cdf = np.cumsum(counts) / counts.sum()
    mid = cdf - counts / counts.sum() / 2  # mid-rank percentile for tied integer counts
    mapped = np.quantile(v, mid)
    mapped = np.maximum.accumulate(mapped)  # keep the mapping monotonic
    missed = viirs[~seen].sum() / max(v.sum(), 1)
    top_ratio = mapped[-1] / values[-1]
    return {"modis": values, "viirs": mapped, "missed_share": float(missed),
            "top_ratio": float(top_ratio), "n": int(seen.sum())}


def apply_group(table: dict, modis: np.ndarray) -> np.ndarray:
    out = np.interp(modis, table["modis"], table["viirs"])
    above = modis > table["modis"][-1]
    out[above] = modis[above] * table["top_ratio"]
    out[modis <= 0] = 0.0
    return out


def fit_mappings(df: pd.DataFrame, sensor: str, years: tuple[int, int]) -> dict:
    """Fit tables per (biome, season), with biome-level and global fallbacks."""
    train = df[df.year.between(*years) & df.snpp.notna()]
    tables = {"global": fit_group(train[sensor].values, train.snpp.values)}
    for biome, g in train.groupby("biome_num"):
        if (g[sensor] > 0).sum() >= MIN_SAMPLES:
            tables[(biome, None)] = fit_group(g[sensor].values, g.snpp.values)
    for (biome, season), g in train.groupby(["biome_num", "season"], observed=True):
        if (g[sensor] > 0).sum() >= MIN_SAMPLES:
            tables[(biome, season)] = fit_group(g[sensor].values, g.snpp.values)
    return tables


def predict(df: pd.DataFrame, tables: dict, sensor: str) -> tuple[np.ndarray, np.ndarray]:
    located = np.zeros(len(df))
    missed = np.zeros(len(df))
    for (biome, season), idx in df.groupby(["biome_num", "season"], observed=True).indices.items():
        table = tables.get((biome, season)) or tables.get((biome, None)) or tables["global"]
        located[idx] = apply_group(table, df[sensor].values[idx].astype(float))
        missed[idx] = table["missed_share"]
    return located, located * (1 + missed)


def scores(pred: np.ndarray, obs: np.ndarray) -> dict:
    pred, obs = np.asarray(pred, float), np.asarray(obs, float)
    return {
        "bias_pct": round(100 * (pred.sum() - obs.sum()) / obs.sum(), 1),
        "rmse": round(float(np.sqrt(np.mean((pred - obs) ** 2))), 2),
        "r": round(float(np.corrcoef(pred, obs)[0, 1]), 3),
        "r_log": round(float(np.corrcoef(np.log1p(pred), np.log1p(obs))[0, 1]), 3),
        "median_abs_pct_err": round(float(np.median(np.abs(pred - obs) / np.maximum(obs, 1)) * 100), 1),
        "n": int(len(obs)),
    }


def evaluate(df: pd.DataFrame, tables: dict) -> dict:
    train = df[df.year.between(*TRAIN) & df.snpp.notna()]
    k = train.snpp.sum() / train.aqua.sum()  # single global multiplier, the naive reference
    test = df[df.year.between(*TEST)].copy()
    test["snpp"] = test.snpp.fillna(0)
    test["located"], test["total"] = predict(test, tables, "aqua")
    test["naive"] = k * test.aqua

    seen = test[test.aqua > 0]
    metrics = {
        "global_multiplier_k": round(float(k), 3),
        "cell_month": {
            "raw_modis": scores(seen.aqua, seen.snpp),
            "global_multiplier": scores(seen.naive, seen.snpp),
            "quantile_mapping": scores(seen.located, seen.snpp),
        },
    }
    for level in ["biome_name", "country_iso3", "ca_ecozone"]:
        grouped = test.dropna(subset=[level]).groupby([level, "year"])[["aqua", "naive", "total", "snpp"]].sum()
        grouped = grouped[grouped.snpp >= 100]  # skip region-years with too few fires to score fairly
        metrics[f"{level}_year"] = {
            "raw_modis": scores(grouped.aqua, grouped.snpp),
            "global_multiplier": scores(grouped.naive, grouped.snpp),
            "quantile_mapping": scores(grouped.total, grouped.snpp),
        }
    per_biome = {}
    for biome, g in test.groupby("biome_name"):
        if g.snpp.sum() >= 1000:
            yearly = g.groupby("year")[["naive", "total", "snpp"]].sum()
            per_biome[biome] = {
                "global_multiplier_bias_pct": round(100 * (yearly.naive.sum() / yearly.snpp.sum() - 1), 1),
                "quantile_mapping_bias_pct": round(100 * (yearly.total.sum() / yearly.snpp.sum() - 1), 1),
            }
    metrics["per_biome_test_bias"] = per_biome
    return metrics


def backcast(df: pd.DataFrame, aqua_tables: dict, terra_tables: dict) -> pd.DataFrame:
    """VIIRS-equivalent counts for every cell-month 2001-2024 from the MODIS sensor available."""
    aqua_era = (df.year > 2002) | ((df.year == 2002) & (df.month >= 7))
    parts = []
    for sensor, tables, rows in (("aqua", aqua_tables, df[aqua_era & (df.aqua > 0)]),
                                 ("terra", terra_tables, df[~aqua_era & (df.terra > 0)])):
        rows = rows.copy()
        rows["located"], rows["total"] = predict(rows, tables, sensor)
        rows["sensor"] = sensor
        rows["modis_n"] = rows[sensor]
        parts.append(rows)
    out = pd.concat(parts)
    return out.rename(columns={"located": "viirs_eq_located", "total": "viirs_eq_total", "snpp": "viirs_obs"})[
        ["year", "month", "row_idx", "col_idx", "sensor", "modis_n", "viirs_eq_located", "viirs_eq_total", "viirs_obs"]
    ]


def mapping_table(tables: dict, sensor: str) -> pd.DataFrame:
    rows = []
    for key, t in tables.items():
        biome, season = ("all", "all") if key == "global" else (key[0], key[1] or "all")
        for m, v in zip(t["modis"], t["viirs"]):
            rows.append({"sensor": sensor, "biome_num": str(biome), "season": str(season), "modis": int(m),
                         "viirs_eq": float(v), "missed_share": t["missed_share"], "n_train": t["n"]})
    return pd.DataFrame(rows)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    df = load_cell_months()
    print(f"{len(df):,} cell-months loaded (2001-2024)")

    aqua_tables = fit_mappings(df, "aqua", TRAIN)
    terra_tables = fit_mappings(df, "terra", TERRA_TRAIN)
    print(f"Aqua tables: {len(aqua_tables) - 1} groups + global; Terra: {len(terra_tables) - 1} + global")

    metrics = evaluate(df, aqua_tables)
    (OUT / "baseline_metrics.json").write_text(json.dumps(metrics, indent=2))

    result = backcast(df, aqua_tables, terra_tables)
    result.to_parquet(OUT / "baseline_cellmonth.parquet", index=False)
    pd.concat([mapping_table(aqua_tables, "aqua"), mapping_table(terra_tables, "terra")]).to_parquet(
        OUT / "baseline_mapping.parquet", index=False)

    print(f"\nGlobal multiplier k = {metrics['global_multiplier_k']}")
    for level in ["cell_month", "biome_name_year", "country_iso3_year", "ca_ecozone_year"]:
        print(f"\nHold-out 2021-2024, {level}:")
        for method, s in metrics[level].items():
            print(f"  {method:<18} bias {s['bias_pct']:>7}%  r {s['r']:>6}  r_log {s['r_log']:>6}  "
                  f"median |err| {s['median_abs_pct_err']:>6}%  (n={s['n']:,})")

    yearly = result.groupby("year")[["modis_n", "viirs_eq_total", "viirs_obs"]].sum()
    viirs_all = duckdb.sql(f"SELECT year(date) y, sum(n_det) v FROM read_parquet('{GRID}/viirs-snpp/*.parquet') GROUP BY 1").df().set_index("y").v
    yearly["viirs_all_cells"] = viirs_all
    print("\nGlobal annual totals (millions of detections):")
    print((yearly / 1e6).round(2).to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
