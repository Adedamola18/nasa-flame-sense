"""ML harmonisation: predict Suomi-NPP VIIRS fire counts from MODIS observations and context.

Unit: one 0.25-degree cell x local solar day x pass (D/N). Target: S-NPP confident detections.

The prediction universe is defined from MODIS alone, so 2002-2011 rows are selected exactly like
2012-2024 rows (no target leakage, no selection bias):
  Tier A  Aqua saw fire in this cell on this pass                -> model A
  Tier B  no Aqua detection here, but any MODIS activity within  -> model B (missed fires)
          a 5x5 block on days d-3..d+1
  Tier C  VIIRS fires outside the universe (~3%)                 -> per biome x season share R_C,
                                                                    applied to totals like the baseline

Features use only MODIS, geography and season. There is no year feature: Aqua's late-mission orbit
drift is learned from the observed overpass hour of each detection, not from the calendar.

Stages (each skips work that already exists unless --force):
  features  per-year feature files            data/ml/features/{year}.parquet (+ tierC_{year})
  sample    stratified, weighted samples      data/ml/sample/{A,B}_{split}.parquet
  train     LightGBM Tweedie models           data/harmonized/ml_models/
  predict   cell-day predictions, all years   data/ml/preds/{final,inner}_{year}.parquet
  evaluate  scoreboard vs the baseline        data/harmonized/ml_metrics.json
  merge     per-biome ML / baseline choice    data/harmonized/{ml,merged}_cellmonth.parquet

Usage:
  python pipeline/05b_harmonize_ml.py --stage features --years 2019
  python pipeline/05b_harmonize_ml.py --stage all
"""

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

import duckdb
import lightgbm as lgb
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent
GRID = ROOT / "data" / "grid" / "global"
REGIONS = ROOT / "data" / "regions" / "cells.parquet"
ML = ROOT / "data" / "ml"
HARM = ROOT / "data" / "harmonized"
MODELS = HARM / "ml_models"

AQUA_START = "2002-07-04"
YEARS = range(2002, 2025)
INNER_TRAIN, INNER_VAL = (2012, 2018), (2019, 2020)
FINAL_TRAIN, TEST = (2012, 2020), (2021, 2024)

_spec = importlib.util.spec_from_file_location("baseline", ROOT / "pipeline" / "05_harmonize_baseline.py")
baseline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(baseline)

FEATURES = [
    "aq_n", "aq_na", "aq_frp", "aq_frpmax", "aq_frp_per_det", "aq_scan", "aq_track", "aq_conf", "aq_hour",
    "aqo_n", "aqo_frp", "td_n", "td_frp", "tn_n", "terra_hour", "aqua_hour_nominal",
    "nb8_n", "nb8_frp", "nb8_max", "ring24_any", "self_any",
    "lag1_any", "lag2_any", "lag3_any", "lead1_any", "lag1_aq_n", "lag9_any",
    "biome_num", "realm", "pass", "lat", "abs_lat", "doy_sin", "doy_cos",
    "clim_rate", "clim_active", "clim_fpd",
]
CATEGORICAL = ["biome_num", "realm", "pass"]
MONOTONE = {
    "A": {"aq_n": 1, "aq_na": 1},
    "B": {"nb8_n": 1, "ring24_any": 1, "self_any": 1, "lag1_any": 1, "lead1_any": 1, "lag9_any": 1},
}
TWEEDIE_POWERS = {"A": [1.1, 1.2, 1.35], "B": [1.3, 1.5, 1.7]}
SAMPLE_TARGET = {"A": 25_000, "B": 30_000}  # rows per stratum per split
LGB_PARAMS = dict(
    objective="tweedie", learning_rate=0.05, num_leaves=63, min_data_in_leaf=200,
    feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1, lambda_l2=1.0, max_bin=255,
    cat_smooth=20, deterministic=True, seed=42, num_threads=8, verbose=-1,
    monotone_constraints_method="advanced",
)


# ---------------------------------------------------------------- shared tables

def connect() -> duckdb.DuckDBPyConnection:
    (ML / "tmp").mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.sql(f"SET memory_limit='8GB'; SET threads=8; SET temp_directory='{ML / 'tmp'}'; "
            "SET preserve_insertion_order=false")
    return con


def shared_tables(con: duckdb.DuckDBPyConnection) -> None:
    """Cell climatology, nominal Aqua overpass hour and sensor-outage dates (cached)."""
    clim, hours, outages = ML / "cell_clim.parquet", ML / "nominal_hours.parquet", ML / "outages.parquet"
    if not clim.exists():
        # Land-cover proxy from MODIS only (no target information): how often and how hot a cell burns.
        con.sql(f"""COPY (
            SELECT row_idx, col_idx,
                   sum(n_det) / (DATE '2024-12-31' - DATE '{AQUA_START}' + 1)::DOUBLE AS clim_rate,
                   count(DISTINCT date) FILTER (WHERE n_det > 0) / (DATE '2024-12-31' - DATE '{AQUA_START}' + 1)::DOUBLE AS clim_active,
                   sum(frp_sum) / nullif(sum(n_det), 0) AS clim_fpd
            FROM read_parquet('{GRID}/modis/*.parquet')
            WHERE satellite = 'Aqua' GROUP BY ALL
        ) TO '{clim}' (FORMAT parquet)""")
    if not hours.exists():
        con.sql(f"""COPY (
            WITH d AS (
                SELECT date, daynight AS pass, median(solar_hour) AS h
                FROM read_parquet('{GRID}/modis/*.parquet') WHERE satellite = 'Aqua' GROUP BY ALL
            )
            SELECT date, pass, avg(h) OVER (PARTITION BY pass ORDER BY date
                   RANGE BETWEEN INTERVAL 7 DAY PRECEDING AND INTERVAL 7 DAY FOLLOWING) AS aqua_hour_nominal
            FROM d
        ) TO '{hours}' (FORMAT parquet)""")
    if not outages.exists():
        con.sql(f"""COPY (
            WITH d AS (
                SELECT date, satellite, sum(n_det) AS n FROM read_parquet('{GRID}/modis/*.parquet') WHERE satellite = 'Aqua' GROUP BY ALL
                UNION ALL
                SELECT date, 'N' AS satellite, sum(n_det) FROM read_parquet('{GRID}/viirs-snpp/*.parquet') GROUP BY ALL
            ), all_days AS (
                SELECT s.satellite, g.date::DATE AS date
                FROM (SELECT DISTINCT satellite FROM d) s,
                     generate_series(DATE '{AQUA_START}', DATE '2024-12-31', INTERVAL 1 DAY) g(date)
            ), f AS (
                SELECT a.satellite, a.date, coalesce(d.n, 0) AS n FROM all_days a LEFT JOIN d USING (satellite, date)
            )
            SELECT satellite, date, n, median(n) OVER (PARTITION BY satellite ORDER BY date
                   RANGE BETWEEN INTERVAL 15 DAY PRECEDING AND INTERVAL 15 DAY FOLLOWING) AS rolling_median
            FROM f QUALIFY n < 0.25 * rolling_median OR (satellite = 'N' AND date < DATE '2012-01-20')
        ) TO '{outages}' (FORMAT parquet)""")


# ---------------------------------------------------------------- stage: features

def build_features(con: duckdb.DuckDBPyConnection, year: int, force: bool) -> None:
    out = ML / "features" / f"{year}.parquet"
    if out.exists() and not force:
        print(f"skip features {year}")
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    start = max(pd.Timestamp(f"{year}-01-01"), pd.Timestamp(AQUA_START)).date()
    end = pd.Timestamp(f"{year}-12-31").date()
    modis_files = [str(GRID / "modis" / f"{y}.parquet") for y in (year - 1, year, year + 1) if (GRID / "modis" / f"{y}.parquet").exists()]
    snpp_files = [str(GRID / "viirs-snpp" / f"{y}.parquet") for y in (year - 1, year, year + 1) if (GRID / "viirs-snpp" / f"{y}.parquet").exists()]

    # 1. MODIS pivot: one row per cell-day, with Aqua day/night and Terra day/night column groups.
    def aq(p: str) -> str:
        f = f"satellite = 'Aqua' AND daynight = '{p}'"
        k = p.lower()
        return f"""sum(n_det) FILTER (WHERE {f}) AS aq{k}_n, sum(n_det_all) FILTER (WHERE {f}) AS aq{k}_na,
                   sum(frp_sum) FILTER (WHERE {f}) AS aq{k}_frp, max(frp_max) FILTER (WHERE {f}) AS aq{k}_frpmax,
                   avg(scan_mean) FILTER (WHERE {f}) AS aq{k}_scan, avg(track_mean) FILTER (WHERE {f}) AS aq{k}_track,
                   avg(conf_mean) FILTER (WHERE {f}) AS aq{k}_conf, avg(solar_hour) FILTER (WHERE {f}) AS aq{k}_hour"""
    con.sql(f"""CREATE OR REPLACE TEMP TABLE m AS
        SELECT date, row_idx, col_idx, {aq('D')}, {aq('N')},
               sum(n_det) FILTER (WHERE satellite = 'Terra' AND daynight = 'D') AS td_n,
               sum(frp_sum) FILTER (WHERE satellite = 'Terra' AND daynight = 'D') AS td_frp,
               avg(solar_hour) FILTER (WHERE satellite = 'Terra' AND daynight = 'D') AS td_hour,
               sum(n_det) FILTER (WHERE satellite = 'Terra' AND daynight = 'N') AS tn_n,
               sum(n_det_all) AS any_all
        FROM read_parquet({modis_files})
        WHERE date BETWEEN DATE '{start}' - 3 AND DATE '{end}' + 1
        GROUP BY ALL""")

    # 2. Universe: every cell-day within a 5x5 block and days d-3..d+1 of any MODIS activity.
    #    Built from MODIS only; no VIIRS path appears in this query.
    con.sql(f"""CREATE OR REPLACE TEMP TABLE u AS
        SELECT DISTINCT m.date + k::INTEGER AS date, m.row_idx + dr AS row_idx, (m.col_idx + dc + 1440) % 1440 AS col_idx
        FROM m, range(-2, 3) a(dr), range(-2, 3) b(dc), range(-1, 4) t(k)
        WHERE m.any_all > 0 AND m.row_idx + dr BETWEEN 0 AND 719
          AND m.date + k::INTEGER BETWEEN DATE '{start}' AND DATE '{end}'""")

    # 3. Neighbours on the same day (8-neighbour Aqua per pass; 24-cell ring of any MODIS).
    con.sql("""CREATE OR REPLACE TEMP TABLE nb AS
        SELECT m.date, m.row_idx + dr AS row_idx, (m.col_idx + dc + 1440) % 1440 AS col_idx,
               sum(CASE WHEN abs(dr) <= 1 AND abs(dc) <= 1 THEN coalesce(aqd_n, 0) END) AS nb8_d_n,
               sum(CASE WHEN abs(dr) <= 1 AND abs(dc) <= 1 THEN coalesce(aqd_frp, 0) END) AS nb8_d_frp,
               max(CASE WHEN abs(dr) <= 1 AND abs(dc) <= 1 THEN coalesce(aqd_n, 0) END) AS nb8_d_max,
               sum(CASE WHEN abs(dr) <= 1 AND abs(dc) <= 1 THEN coalesce(aqn_n, 0) END) AS nb8_n_n,
               sum(CASE WHEN abs(dr) <= 1 AND abs(dc) <= 1 THEN coalesce(aqn_frp, 0) END) AS nb8_n_frp,
               max(CASE WHEN abs(dr) <= 1 AND abs(dc) <= 1 THEN coalesce(aqn_n, 0) END) AS nb8_n_max,
               sum(any_all) AS ring24_any
        FROM m, range(-2, 3) a(dr), range(-2, 3) b(dc)
        WHERE NOT (dr = 0 AND dc = 0) AND m.row_idx + dr BETWEEN 0 AND 719
        GROUP BY ALL""")

    # 4. Time context for the same cell, and a 3x3 sum over the previous three days.
    con.sql("""CREATE OR REPLACE TEMP TABLE lg AS
        SELECT m.date + k::INTEGER AS date, row_idx, col_idx,
               sum(any_all) FILTER (WHERE k = 1) AS lag1_any, sum(any_all) FILTER (WHERE k = 2) AS lag2_any,
               sum(any_all) FILTER (WHERE k = 3) AS lag3_any, sum(any_all) FILTER (WHERE k = -1) AS lead1_any,
               sum(aqd_n) FILTER (WHERE k = 1) AS lag1_d_n, sum(aqn_n) FILTER (WHERE k = 1) AS lag1_n_n
        FROM m, range(-1, 4) t(k) WHERE k <> 0 GROUP BY ALL""")
    con.sql("""CREATE OR REPLACE TEMP TABLE l9 AS
        SELECT m.date + k::INTEGER AS date, m.row_idx + dr AS row_idx, (m.col_idx + dc + 1440) % 1440 AS col_idx,
               sum(any_all) AS lag9_any
        FROM m, range(-1, 2) a(dr), range(-1, 2) b(dc), range(1, 4) t(k)
        WHERE m.row_idx + dr BETWEEN 0 AND 719 GROUP BY ALL""")

    # 5. Target: S-NPP per cell-day and pass (only from 2012; NULL before).
    has_snpp = year >= 2012
    if has_snpp:
        con.sql(f"""CREATE OR REPLACE TEMP TABLE sn AS
            SELECT date, row_idx, col_idx,
                   sum(n_det) FILTER (WHERE daynight = 'D') AS snd_n, sum(n_det) FILTER (WHERE daynight = 'N') AS snn_n
            FROM read_parquet({snpp_files})
            WHERE date BETWEEN DATE '{start}' AND DATE '{end}' GROUP BY ALL""")
    else:
        con.sql("CREATE OR REPLACE TEMP TABLE sn AS SELECT NULL::DATE AS date, NULL::SMALLINT AS row_idx, "
                "NULL::SMALLINT AS col_idx, NULL::BIGINT AS snd_n, NULL::BIGINT AS snn_n WHERE false")

    def by_pass(d: str, n: str) -> str:
        return f"CASE p.pass WHEN 'D' THEN {d} ELSE {n} END"

    target = f"coalesce({by_pass('sn.snd_n', 'sn.snn_n')}, 0)" if has_snpp else "NULL::BIGINT"
    con.sql(f"""COPY (
        SELECT u.date, u.row_idx, u.col_idx, p.pass,
               coalesce({by_pass('m.aqd_n', 'm.aqn_n')}, 0) AS aq_n,
               coalesce({by_pass('m.aqd_na', 'm.aqn_na')}, 0) AS aq_na,
               coalesce({by_pass('m.aqd_frp', 'm.aqn_frp')}, 0) AS aq_frp,
               coalesce({by_pass('m.aqd_frpmax', 'm.aqn_frpmax')}, 0) AS aq_frpmax,
               {by_pass('m.aqd_frp', 'm.aqn_frp')} / nullif({by_pass('m.aqd_n', 'm.aqn_n')}, 0) AS aq_frp_per_det,
               {by_pass('m.aqd_scan', 'm.aqn_scan')} AS aq_scan,
               {by_pass('m.aqd_track', 'm.aqn_track')} AS aq_track,
               {by_pass('m.aqd_conf', 'm.aqn_conf')} AS aq_conf,
               {by_pass('m.aqd_hour', 'm.aqn_hour')} AS aq_hour,
               coalesce({by_pass('m.aqn_n', 'm.aqd_n')}, 0) AS aqo_n,
               coalesce({by_pass('m.aqn_frp', 'm.aqd_frp')}, 0) AS aqo_frp,
               coalesce(m.td_n, 0) AS td_n, coalesce(m.td_frp, 0) AS td_frp, coalesce(m.tn_n, 0) AS tn_n,
               m.td_hour AS terra_hour, h.aqua_hour_nominal,
               coalesce({by_pass('nb.nb8_d_n', 'nb.nb8_n_n')}, 0) AS nb8_n,
               coalesce({by_pass('nb.nb8_d_frp', 'nb.nb8_n_frp')}, 0) AS nb8_frp,
               coalesce({by_pass('nb.nb8_d_max', 'nb.nb8_n_max')}, 0) AS nb8_max,
               coalesce(nb.ring24_any, 0) AS ring24_any, coalesce(m.any_all, 0) AS self_any,
               coalesce(lg.lag1_any, 0) AS lag1_any, coalesce(lg.lag2_any, 0) AS lag2_any,
               coalesce(lg.lag3_any, 0) AS lag3_any, coalesce(lg.lead1_any, 0) AS lead1_any,
               coalesce({by_pass('lg.lag1_d_n', 'lg.lag1_n_n')}, 0) AS lag1_aq_n,
               coalesce(l9.lag9_any, 0) AS lag9_any,
               r.biome_num, r.realm,
               ((u.row_idx + 0.5) * 0.25 - 90)::DOUBLE AS lat, abs((u.row_idx + 0.5) * 0.25 - 90)::DOUBLE AS abs_lat,
               sin(2 * pi() * (dayofyear(u.date) + CASE WHEN u.row_idx < 360 THEN 182 ELSE 0 END) / 365.25) AS doy_sin,
               cos(2 * pi() * (dayofyear(u.date) + CASE WHEN u.row_idx < 360 THEN 182 ELSE 0 END) / 365.25) AS doy_cos,
               c.clim_rate, c.clim_active, c.clim_fpd,
               CASE WHEN coalesce({by_pass('m.aqd_n', 'm.aqn_n')}, 0) > 0 THEN 'A' ELSE 'B' END AS tier,
               {target} AS snpp_n,
               (o_s.date IS NOT NULL) AS snpp_outage, (o_a.date IS NOT NULL) AS aqua_outage
        FROM u CROSS JOIN (VALUES ('D'), ('N')) p(pass)
        LEFT JOIN m USING (date, row_idx, col_idx)
        LEFT JOIN nb USING (date, row_idx, col_idx)
        LEFT JOIN lg USING (date, row_idx, col_idx)
        LEFT JOIN l9 USING (date, row_idx, col_idx)
        LEFT JOIN sn USING (date, row_idx, col_idx)
        LEFT JOIN read_parquet('{REGIONS}') r USING (row_idx, col_idx)
        LEFT JOIN read_parquet('{ML / 'cell_clim.parquet'}') c USING (row_idx, col_idx)
        LEFT JOIN read_parquet('{ML / 'nominal_hours.parquet'}') h ON h.date = u.date AND h.pass = p.pass
        LEFT JOIN (SELECT date FROM read_parquet('{ML / 'outages.parquet'}') WHERE satellite = 'N') o_s ON o_s.date = u.date
        LEFT JOIN (SELECT date FROM read_parquet('{ML / 'outages.parquet'}') WHERE satellite = 'Aqua') o_a ON o_a.date = u.date
    ) TO '{out}' (FORMAT parquet, COMPRESSION zstd)""")

    # 6. Tier C: S-NPP fires outside the universe, per cell-month and pass.
    if has_snpp:
        con.sql(f"""COPY (
            WITH s AS (
                SELECT date, row_idx, col_idx, 'D' AS pass, snd_n AS n FROM sn WHERE snd_n > 0
                UNION ALL SELECT date, row_idx, col_idx, 'N', snn_n FROM sn WHERE snn_n > 0
            )
            SELECT year(s.date) AS year, month(s.date) AS month, s.row_idx, s.col_idx, s.pass, sum(s.n) AS snpp_n
            FROM s ANTI JOIN u USING (date, row_idx, col_idx)
            GROUP BY ALL
        ) TO '{ML / 'features' / f'tierC_{year}.parquet'}' (FORMAT parquet)""")

    stats = con.sql(f"""SELECT count(*) n, count(*) FILTER (WHERE tier = 'A') a, sum(snpp_n) s
                        FROM read_parquet('{out}')""").fetchone()
    print(f"features {year}: {stats[0]:,} rows, tier A {stats[1]:,}, S-NPP in universe {stats[2] or 0:,}", flush=True)


# ---------------------------------------------------------------- stage: sample

def build_samples(con: duckdb.DuckDBPyConnection, force: bool) -> None:
    """Deterministic hash-stratified samples with inverse-probability weights."""
    out_dir = ML / "sample"
    out_dir.mkdir(parents=True, exist_ok=True)
    files = [str(ML / "features" / f"{y}.parquet") for y in range(FINAL_TRAIN[0], FINAL_TRAIN[1] + 1)]
    strata = {
        "A": "coalesce(biome_num, 0)::VARCHAR || '|' || pass || '|' || CASE WHEN aq_n = 1 THEN '1' WHEN aq_n <= 3 THEN '2-3' "
             "WHEN aq_n <= 9 THEN '4-9' ELSE '10+' END",
        "B": "coalesce(biome_num, 0)::VARCHAR || '|' || (snpp_n > 0)::VARCHAR || '|' || CASE WHEN nb8_n > 0 THEN 'nbr' "
             "WHEN lag1_any + lag2_any + lag3_any + lead1_any + lag9_any > 0 THEN 'lag' ELSE 'ring' END",
    }
    splits = {"inner_train": INNER_TRAIN, "val": INNER_VAL, "final_train": FINAL_TRAIN}
    for tier, stratum in strata.items():
        for split, (y0, y1) in splits.items():
            out = out_dir / f"{tier}_{split}.parquet"
            if out.exists() and not force:
                print(f"skip sample {tier} {split}")
                continue
            base = f"""SELECT *, {stratum} AS stratum FROM read_parquet({files})
                       WHERE tier = '{tier}' AND year(date) BETWEEN {y0} AND {y1} AND NOT snpp_outage"""
            con.sql(f"""COPY (
                WITH b AS ({base}),
                     c AS (SELECT stratum, least(1.0, {SAMPLE_TARGET[tier]} / count(*)::DOUBLE) AS rate FROM b GROUP BY ALL)
                SELECT b.*, 1.0 / c.rate AS weight
                FROM b JOIN c USING (stratum)
                WHERE (hash(b.row_idx, b.col_idx, b.date, b.pass) % 1000000) < c.rate * 1000000
            ) TO '{out}' (FORMAT parquet)""")
            n, w = con.sql(f"SELECT count(*), sum(weight * snpp_n) FROM '{out}'").fetchone()
            print(f"sample {tier} {split}: {n:,} rows, weighted S-NPP {w:,.0f}", flush=True)


# ---------------------------------------------------------------- stage: train

def frame(df: pd.DataFrame) -> pd.DataFrame:
    leaked = [f for f in FEATURES if re.search(r"snpp|viirs|year", f)]
    assert not leaked, f"target-derived features: {leaked}"
    X = df[FEATURES].copy()
    numeric = [f for f in FEATURES if f not in CATEGORICAL]
    X[numeric] = X[numeric].apply(pd.to_numeric, errors="coerce").astype("float32")  # DECIMAL/HUGEINT -> float
    X["biome_num"] = pd.Categorical(X["biome_num"].astype("float").astype("Int64"), categories=list(range(1, 15)) + [99])
    X["realm"] = pd.Categorical(X["realm"], categories=["Afrotropic", "Antarctica", "Australasia", "Indomalayan",
                                                        "Nearctic", "Neotropic", "Oceania", "Palearctic"])
    X["pass"] = pd.Categorical(X["pass"], categories=["D", "N"])
    return X


def monotone_vector(tier: str) -> list[int]:
    return [MONOTONE[tier].get(f, 0) for f in FEATURES]


def fit(tier: str, train: pd.DataFrame, power: float, rounds: int, valid: pd.DataFrame | None = None) -> lgb.Booster:
    params = {**LGB_PARAMS, "tweedie_variance_power": power, "monotone_constraints": monotone_vector(tier)}
    dtrain = lgb.Dataset(frame(train), train.snpp_n, weight=train.weight, categorical_feature=CATEGORICAL)
    if valid is None:
        return lgb.train(params, dtrain, num_boost_round=rounds)
    dvalid = lgb.Dataset(frame(valid), valid.snpp_n, weight=valid.weight, reference=dtrain)
    return lgb.train(params, dtrain, num_boost_round=rounds, valid_sets=[dvalid],
                     callbacks=[lgb.early_stopping(100, verbose=False)])


def poisson_deviance(y: np.ndarray, mu: np.ndarray, w: np.ndarray) -> float:
    mu = np.maximum(mu, 1e-9)
    term = np.where(y > 0, y * np.log(np.where(y > 0, y, 1) / mu), 0.0) - (y - mu)
    return float(2 * np.sum(w * term) / np.sum(w))


CALIBRATION_TOLERANCE = 0.03  # validation sum(pred)/sum(obs) must be within +-3%


def is_better(candidate: dict, best: dict | None) -> bool:
    """Lowest Poisson deviance among calibrated candidates; the output of this project is fire
    totals, so a setting that misses the validation total by more than 3% loses to one that doesn't."""
    if best is None:
        return True
    ok = lambda c: abs(c["ratio"] - 1) <= CALIBRATION_TOLERANCE
    if ok(candidate) != ok(best):
        return ok(candidate)
    if not ok(candidate):
        return abs(candidate["ratio"] - 1) < abs(best["ratio"] - 1)
    return candidate["deviance"] < best["deviance"]


def train_models(force: bool) -> dict:
    MODELS.mkdir(parents=True, exist_ok=True)
    meta_path = MODELS / "meta.json"
    if meta_path.exists() and not force:
        print("skip train")
        return json.loads(meta_path.read_text())
    meta = {"features": FEATURES, "categorical": CATEGORICAL}
    for tier in ("A", "B"):
        train = pd.read_parquet(ML / "sample" / f"{tier}_inner_train.parquet")
        val = pd.read_parquet(ML / "sample" / f"{tier}_val.parquet")
        best = None
        for power in TWEEDIE_POWERS[tier]:
            model = fit(tier, train, power, 3000, val)
            pred = model.predict(frame(val), num_iteration=model.best_iteration)
            ratio = float((pred * val.weight).sum() / (val.snpp_n * val.weight).sum())
            # Tweedie deviances from different powers are on different scales, so candidates are
            # compared on one common yardstick: weighted Poisson deviance on the validation years.
            score = poisson_deviance(val.snpp_n.to_numpy(float), pred, val.weight.to_numpy(float))
            print(f"  {tier} p={power}: val Poisson deviance {score:.4f} (Tweedie {model.best_score['valid_0']['tweedie']:.4f}), "
                  f"iters {model.best_iteration}, val sum(pred)/sum(obs) {ratio:.3f}", flush=True)
            candidate = {"power": power, "deviance": score, "iters": model.best_iteration, "ratio": ratio, "model": model}
            if is_better(candidate, best):
                best = candidate
        best["model"].save_model(str(MODELS / f"{tier}_inner.txt"), num_iteration=best["iters"])
        final_train = pd.read_parquet(ML / "sample" / f"{tier}_final_train.parquet")
        rounds = int(best["iters"] * 1.1)
        fit(tier, final_train, best["power"], rounds).save_model(str(MODELS / f"{tier}_final.txt"))
        meta[tier] = {k: v for k, v in best.items() if k != "model"} | {"final_rounds": rounds}
        print(f"model {tier}: Tweedie p={best['power']}, {rounds} rounds (final)", flush=True)
    meta_path.write_text(json.dumps(meta, indent=2))
    return meta


# ---------------------------------------------------------------- stage: predict

def predict_year(year: int, which: str, force: bool) -> None:
    out = ML / "preds" / f"{which}_{year}.parquet"
    if out.exists() and not force:
        print(f"skip predict {which} {year}")
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    models = {t: lgb.Booster(model_file=str(MODELS / f"{t}_{which}.txt")) for t in ("A", "B")}
    keep = ["date", "row_idx", "col_idx", "pass", "tier", "aq_n", "snpp_n", "snpp_outage"]
    parts = []
    for batch in pq.ParquetFile(ML / "features" / f"{year}.parquet").iter_batches(batch_size=2_000_000):
        df = batch.to_pandas()
        pred = np.zeros(len(df))
        for tier, model in models.items():
            idx = (df.tier == tier).values
            if idx.any():
                pred[idx] = model.predict(frame(df[idx]))
        part = df[keep].copy()
        part["pred"] = pred.astype("float32")
        parts.append(part)
    pd.concat(parts).to_parquet(out, index=False)
    print(f"predict {which} {year}: done", flush=True)


# ---------------------------------------------------------------- cell-months and evaluation

RC_MIN_INSIDE = 1000  # S-NPP detections inside the universe needed to trust a group's own R_C
RC_CAP = 1.0          # tier C may at most double a group's located total


def outage_dates_sql() -> str:
    """Dates on which either sensor was out (S-NPP counted only from its first data, 20 Jan 2012)."""
    return (f"(SELECT date FROM read_parquet('{ML / 'outages.parquet'}') "
            "WHERE satellite = 'Aqua' OR date >= DATE '2012-01-20')")


def tier_c_share(con: duckdb.DuckDBPyConnection, which: str, years: tuple[int, int]) -> tuple[pd.DataFrame, float]:
    """R_C per biome x season: S-NPP outside the universe / S-NPP inside, over the training years.

    Uses observed S-NPP counts only (from the feature files), so it needs no model predictions;
    `which` is kept for call-site symmetry. Groups with fewer than RC_MIN_INSIDE inside detections
    fall back to their biome, then to the global ratio (e.g. winter tundra has 27 inside vs 19,458
    outside, mostly persistent sources, which would otherwise multiply ML totals by ~720).
    Returns the full biome x season table and the global ratio.
    """
    y0, y1 = years
    feats = [str(ML / "features" / f"{y}.parquet") for y in range(y0, y1 + 1)]
    tierc = [str(ML / "features" / f"tierC_{y}.parquet") for y in range(y0, y1 + 1)]
    df = con.sql(f"""
        WITH inside AS (
            SELECT year(date) AS y, month(date) AS m, row_idx, col_idx, sum(snpp_n) AS n
            FROM read_parquet({feats}) WHERE NOT snpp_outage GROUP BY ALL
        ), outside AS (
            SELECT year AS y, month AS m, row_idx, col_idx, sum(snpp_n) AS n FROM read_parquet({tierc}) GROUP BY ALL
        ), io AS (
            SELECT row_idx, col_idx, m, coalesce(i.n, 0) AS ins, coalesce(o.n, 0) AS outs
            FROM inside i FULL OUTER JOIN outside o USING (y, m, row_idx, col_idx)
        )
        SELECT coalesce(r.biome_num, 0)::INT AS biome_num,
               ['DJF', 'MAM', 'JJA', 'SON'][(((io.m % 12) // 3 + CASE WHEN r.lat < 0 THEN 2 ELSE 0 END) % 4) + 1] AS season,
               sum(ins)::DOUBLE AS ins, sum(outs)::DOUBLE AS outs
        FROM io LEFT JOIN read_parquet('{REGIONS}') r USING (row_idx, col_idx)
        GROUP BY ALL
    """).df()
    r_global = float(df.outs.sum() / df.ins.sum())
    biome = df.groupby("biome_num")[["ins", "outs"]].sum()
    r_biome = {b: (row.outs / row.ins if row.ins >= RC_MIN_INSIDE else r_global) for b, row in biome.iterrows()}
    biomes = sorted(set(biome.index) | set(pd.read_parquet(REGIONS).biome_num.dropna().astype(int)) | {0})
    own = df.set_index(["biome_num", "season"])
    rows = []
    for b in biomes:
        for season in baseline.SEASONS:
            if (b, season) in own.index and own.loc[(b, season), "ins"] >= RC_MIN_INSIDE:
                r, source = own.loc[(b, season), "outs"] / own.loc[(b, season), "ins"], "biome_season"
            elif b in r_biome and biome.loc[b, "ins"] >= RC_MIN_INSIDE:
                r, source = r_biome[b], "biome"
            else:
                r, source = r_global, "global"
            rows.append({"biome_num": b, "season": season, "r_c": min(float(r), RC_CAP), "source": source})
    return pd.DataFrame(rows), min(r_global, RC_CAP)


def ml_cellmonths(con: duckdb.DuckDBPyConnection, which: str, years: range, rc: tuple[pd.DataFrame, float],
                  exclude_outages: bool = False) -> pd.DataFrame:
    """Sum cell-day predictions to cell-months. exclude_outages drops days either sensor was out
    (for scoring on days both observed); the published record keeps every day."""
    missing = [y for y in years if not (ML / "preds" / f"{which}_{y}.parquet").exists()]
    if missing:
        sys.exit(f"missing predictions: run --stage predict --years for {which} {missing} first")
    preds = [str(ML / "preds" / f"{which}_{y}.parquet") for y in years]
    where = f"WHERE date NOT IN {outage_dates_sql()}" if exclude_outages else ""
    cm = con.sql(f"""
        SELECT year(date) AS year, month(date) AS month, row_idx, col_idx,
               sum(pred) AS ml_located, sum(pred) FILTER (WHERE tier = 'B') / nullif(sum(pred), 0) AS tierB_share,
               sum(aq_n) AS aqua_ml
        FROM read_parquet({preds}) {where} GROUP BY ALL
    """).df()
    regions = pd.read_parquet(REGIONS)[["row_idx", "col_idx", "lat", "biome_num"]]
    cm = cm.merge(regions, on=["row_idx", "col_idx"], how="left")
    season = (cm.month % 12) // 3
    season = np.where(cm.lat.fillna(1) < 0, (season + 2) % 4, season)
    cm["season"] = np.array(baseline.SEASONS)[season]
    cm["biome_key"] = cm.biome_num.fillna(0).astype(int)
    table, r_global = rc
    table = table.rename(columns={"biome_num": "biome_key"})[["biome_key", "season", "r_c"]]
    cm = cm.merge(table, on=["biome_key", "season"], how="left")
    cm["ml_total"] = cm.ml_located * (1 + cm.r_c.fillna(r_global))
    return cm[["year", "month", "row_idx", "col_idx", "ml_located", "ml_total", "tierB_share", "aqua_ml"]]


def eval_cell_months(con: duckdb.DuckDBPyConnection, years: range) -> pd.DataFrame:
    """Aqua and S-NPP cell-month totals over days on which both sensors observed (scoring truth)."""
    y0, y1 = min(years), max(years)
    df = con.sql(f"""
        WITH a AS (
            SELECT row_idx, col_idx, year(date) AS year, month(date) AS month, sum(n_det) AS aqua
            FROM read_parquet('{GRID}/modis/*.parquet')
            WHERE satellite = 'Aqua' AND year(date) BETWEEN {y0} AND {y1} AND date NOT IN {outage_dates_sql()}
            GROUP BY ALL
        ), v AS (
            SELECT row_idx, col_idx, year(date) AS year, month(date) AS month, sum(n_det) AS snpp
            FROM read_parquet('{GRID}/viirs-snpp/*.parquet')
            WHERE year(date) BETWEEN {y0} AND {y1} AND date NOT IN {outage_dates_sql()}
            GROUP BY ALL
        )
        SELECT row_idx, col_idx, year, month, coalesce(aqua, 0)::INT AS aqua, coalesce(snpp, 0)::INT AS snpp
        FROM a FULL OUTER JOIN v USING (row_idx, col_idx, year, month)
    """).df()
    df = df[(df.aqua > 0) | (df.snpp > 0)]
    regions = pd.read_parquet(REGIONS)[["row_idx", "col_idx", "lat", "biome_num", "biome_name", "country_iso3", "ca_ecozone"]]
    df = df.merge(regions, on=["row_idx", "col_idx"], how="left")
    season = (df.month % 12) // 3
    season = np.where(df.lat < 0, (season + 2) % 4, season)
    df["season"] = pd.Categorical.from_codes(season, baseline.SEASONS)
    return df


def scoreboard(df: pd.DataFrame, years: tuple[int, int], methods: dict[str, tuple[str, str]]) -> dict:
    """Score methods on cell-months (seen subset) and region-years. methods: name -> (located_col, total_col)."""
    t = df[df.year.between(*years)].copy()
    t["snpp"] = t.snpp.fillna(0)
    seen = t[t.aqua > 0]
    out = {"cell_month": {name: baseline.scores(seen[loc].fillna(0), seen.snpp) for name, (loc, _) in methods.items()}}
    out["cell_month_all"] = {name: baseline.scores(t[loc].fillna(0), t.snpp) for name, (loc, _) in methods.items()}
    totals = [tot for _, tot in methods.values()]
    for level in ["biome_name", "country_iso3", "ca_ecozone"]:
        g = t.dropna(subset=[level]).groupby([level, "year"])[totals + ["snpp"]].sum()
        g = g[g.snpp >= 100]
        out[f"{level}_year"] = {name: baseline.scores(g[tot], g.snpp) for name, (_, tot) in methods.items()}
    yearly = t.groupby("year")[totals + ["snpp"]].sum()
    out["global_year_bias_pct"] = {name: {int(y): round(100 * (r[tot] / r.snpp - 1), 1) for y, r in yearly.iterrows()}
                                   for name, (_, tot) in methods.items()}
    per_biome = {}
    for biome, g in t.groupby("biome_name"):
        if g.snpp.sum() >= 1000:
            per_biome[biome] = {name: round(100 * (g[tot].sum() / g.snpp.sum() - 1), 1) for name, (_, tot) in methods.items()}
    out["per_biome_bias_pct"] = per_biome
    return out


def evaluation_frame(con: duckdb.DuckDBPyConnection, which: str, qm_years: tuple[int, int], years: range) -> pd.DataFrame:
    """Score frame: raw MODIS, global multiplier, quantile mapping and ML per cell-month.

    The baseline is fitted on the full training cell-months (as in script 05). Every method is then
    scored on the same days: those on which both Aqua and S-NPP observed, so sensor outages
    (31 days in 2022 and 24 in 2024 had either sensor out) are not mistaken for "no fires"."""
    fit_df = baseline.load_cell_months()
    tables = baseline.fit_mappings(fit_df, "aqua", qm_years)
    train = fit_df[fit_df.year.between(*qm_years) & fit_df.snpp.notna()]
    k = train.snpp.sum() / train.aqua.sum()
    df = eval_cell_months(con, years)
    df["qm_located"], df["qm_total"] = baseline.predict(df, tables, "aqua")
    df["naive"] = k * df.aqua
    df["raw"] = df.aqua.astype(float)
    rc = tier_c_share(con, which, qm_years)
    ml = ml_cellmonths(con, which, years, rc, exclude_outages=True)
    keys = ["year", "month", "row_idx", "col_idx"]
    merged = df.merge(ml, on=keys, how="outer")
    regions = pd.read_parquet(REGIONS)[["row_idx", "col_idx", "biome_name", "country_iso3", "ca_ecozone"]]
    merged = merged.drop(columns=["biome_name", "country_iso3", "ca_ecozone"]).merge(regions, on=["row_idx", "col_idx"], how="left")
    for col in ["aqua", "naive", "raw", "qm_located", "qm_total", "ml_located", "ml_total"]:
        merged[col] = merged[col].fillna(0)
    return merged


METHODS = {"raw_modis": ("raw", "raw"), "global_multiplier": ("naive", "naive"),
           "quantile_mapping": ("qm_located", "qm_total"), "ml": ("ml_located", "ml_total")}


def choose_per_biome(val: pd.DataFrame) -> dict:
    """Per-biome rule fixed on 2019-2020 validation: use ML only where it clearly does no worse."""
    choice = {}
    val = val.copy()
    val["snpp"] = val.snpp.fillna(0)
    for biome, g in val.groupby("biome_name"):
        if g.snpp.sum() < 5000:
            choice[biome] = "quantile_mapping"
            continue
        monthly = g.groupby(["year", "month"])[["qm_total", "ml_total", "snpp"]].sum()
        monthly = monthly[monthly.snpp > 0]
        err = lambda c: float(np.median(np.abs(monthly[c] - monthly.snpp) / monthly.snpp))
        bias = lambda c: abs(monthly[c].sum() / monthly.snpp.sum() - 1)
        seen = g[g.aqua > 0]
        rmse = lambda c: float(np.sqrt(np.mean((seen[c] - seen.snpp) ** 2)))
        ok = err("ml_total") < err("qm_total") and bias("ml_total") <= bias("qm_total") + 0.02 \
            and rmse("ml_located") <= rmse("qm_located")
        choice[biome] = "ml" if ok else "quantile_mapping"
    return choice


def evaluate(con: duckdb.DuckDBPyConnection) -> None:
    meta = json.loads((MODELS / "meta.json").read_text())
    val = evaluation_frame(con, "inner", INNER_TRAIN, range(INNER_VAL[0], INNER_VAL[1] + 1))
    choice = choose_per_biome(val)
    test = evaluation_frame(con, "final", FINAL_TRAIN, range(TEST[0], TEST[1] + 1))
    use_ml = test.biome_name.map(choice).eq("ml")
    test["merged_located"] = np.where(use_ml, test.ml_located, test.qm_located)
    test["merged_total"] = np.where(use_ml, test.ml_total, test.qm_total)
    methods = METHODS | {"merged": ("merged_located", "merged_total")}
    metrics = {
        "test_2021_2024": scoreboard(test, TEST, methods),
        "test_2021": scoreboard(test, (2021, 2021), methods),
        "test_2022_2024": scoreboard(test, (2022, 2024), methods),
        "validation_2019_2020": scoreboard(val, INNER_VAL, METHODS),
        "per_biome_choice": choice,
        "models": {t: meta[t] for t in ("A", "B")},
        "features": FEATURES,
        "outage_days_excluded_from_scoring": con.sql(
            f"SELECT year(date) AS year, count(*) FROM {outage_dates_sql()} GROUP BY 1 HAVING year >= 2019 ORDER BY 1").fetchall(),
        "tier_c_share_final": tier_c_share(con, "final", FINAL_TRAIN)[0].to_dict(orient="records"),
    }
    (HARM / "ml_metrics.json").write_text(json.dumps(metrics, indent=2, default=str))
    print_scoreboard(metrics)


def print_scoreboard(metrics: dict) -> None:
    m = metrics["test_2021_2024"]
    for level in ["cell_month", "cell_month_all", "biome_name_year", "country_iso3_year", "ca_ecozone_year"]:
        print(f"\nHold-out 2021-2024, {level}:")
        for method, s in m[level].items():
            print(f"  {method:<18} bias {s['bias_pct']:>7}%  r {s['r']:>6}  r_log {s['r_log']:>6}  "
                  f"median |err| {s['median_abs_pct_err']:>6}%  (n={s['n']:,})")
    print("\nGlobal annual bias % (test years):")
    for method, years in m["global_year_bias_pct"].items():
        print(f"  {method:<18} " + "  ".join(f"{y}: {b:>6}" for y, b in years.items()))
    print("\nPer-biome choice:", json.dumps(metrics["per_biome_choice"], indent=1))


# ---------------------------------------------------------------- stage: merge (published record)

VIIRS_ERA = 2012          # from here the record uses VIIRS observations, not MODIS-derived estimates
NOAA20_START = "2018-04-01"


def viirs_observed(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """2012-2024 cell-months from observed Suomi-NPP VIIRS. Days S-NPP was out are filled with
    NOAA-20 (same VIIRS instrument, ~50 min later) from April 2018, and with the ML prediction
    for earlier gaps (including 1-19 Jan 2012, before S-NPP's first data)."""
    snpp_out = f"(SELECT date FROM read_parquet('{ML / 'outages.parquet'}') WHERE satellite = 'N')"
    preds = [str(ML / "preds" / f"final_{y}.parquet") for y in range(VIIRS_ERA, 2025)]
    missing = [p for p in preds if not Path(p).exists()]
    if missing:
        sys.exit(f"missing predictions for the outage fill: {missing}")
    df = con.sql(f"""
        WITH s AS (
            SELECT year(date) AS year, month(date) AS month, row_idx, col_idx, sum(n_det) AS snpp
            FROM read_parquet('{GRID}/viirs-snpp/*.parquet')
            WHERE year(date) BETWEEN {VIIRS_ERA} AND 2024 AND date NOT IN {snpp_out} GROUP BY ALL
        ), n20 AS (
            SELECT year(date) AS year, month(date) AS month, row_idx, col_idx, sum(n_det) AS fill_n20
            FROM read_parquet('{GRID}/viirs-jpss1/*.parquet')
            WHERE date BETWEEN DATE '{NOAA20_START}' AND DATE '2024-12-31' AND date IN {snpp_out} GROUP BY ALL
        ), mlf AS (
            SELECT year(date) AS year, month(date) AS month, row_idx, col_idx, sum(pred) AS fill_ml
            FROM read_parquet({preds})
            WHERE date < DATE '{NOAA20_START}' AND date IN {snpp_out} GROUP BY ALL
        )
        SELECT year, month, row_idx, col_idx,
               coalesce(snpp, 0) AS snpp, coalesce(fill_n20, 0) AS fill_n20, coalesce(fill_ml, 0) AS fill_ml
        FROM s FULL OUTER JOIN n20 USING (year, month, row_idx, col_idx)
               FULL OUTER JOIN mlf USING (year, month, row_idx, col_idx)
    """).df()
    total = df.snpp + df.fill_n20 + df.fill_ml
    return pd.DataFrame({
        "year": df.year, "month": df.month, "row_idx": df.row_idx, "col_idx": df.col_idx, "sensor": "viirs",
        "modis_n": np.nan, "viirs_eq_located": total, "viirs_eq_total": total, "viirs_obs": df.snpp,
        "fill_n20": df.fill_n20, "fill_ml": df.fill_ml, "method": "viirs_observed",
    })


def merge_record(con: duckdb.DuckDBPyConnection) -> None:
    """Published record: Terra mapping (to Jun 2002), harmonised MODIS (Jul 2002-2011, ML or quantile
    mapping per biome), then observed VIIRS (2012-2024). Model estimates are not used where VIIRS
    observed, so Aqua's late-mission orbit drift (2022-2024) cannot leak into the record."""
    metrics = json.loads((HARM / "ml_metrics.json").read_text())
    choice = metrics["per_biome_choice"]
    rc = tier_c_share(con, "final", FINAL_TRAIN)
    ml = ml_cellmonths(con, "final", YEARS, rc)
    aqua_start = pd.Timestamp(AQUA_START)
    ml = ml[(ml.year > 2002) | ((ml.year == 2002) & (ml.month >= aqua_start.month))]
    obs = con.sql(f"""SELECT year(date) AS year, month(date) AS month, row_idx, col_idx, sum(n_det) AS viirs_obs
                      FROM read_parquet('{GRID}/viirs-snpp/*.parquet') GROUP BY ALL""").df()
    ml = ml.merge(obs, on=["year", "month", "row_idx", "col_idx"], how="left")
    ml_out = pd.DataFrame({
        "year": ml.year, "month": ml.month, "row_idx": ml.row_idx, "col_idx": ml.col_idx, "sensor": "aqua",
        "modis_n": ml.aqua_ml, "viirs_eq_located": ml.ml_located, "viirs_eq_total": ml.ml_total,
        "viirs_obs": ml.viirs_obs, "tierB_share": ml.tierB_share, "method": "ml",
    })
    ml_out.to_parquet(HARM / "ml_cellmonth.parquet", index=False)  # full 2002-2024 ML series, for validation

    base = pd.read_parquet(HARM / "baseline_cellmonth.parquet")
    regions = pd.read_parquet(REGIONS)[["row_idx", "col_idx", "biome_name"]]
    base = base.merge(regions, on=["row_idx", "col_idx"], how="left")
    ml_out = ml_out.merge(regions, on=["row_idx", "col_idx"], how="left")
    use_ml = lambda d: d.biome_name.map(choice).eq("ml")
    pre_viirs = lambda d: d.year < VIIRS_ERA
    merged = pd.concat([
        base[base.sensor == "terra"].assign(method="baseline_terra"),
        base[(base.sensor == "aqua") & pre_viirs(base) & ~use_ml(base)].assign(method="quantile_mapping"),
        ml_out[pre_viirs(ml_out) & use_ml(ml_out)],
        viirs_observed(con),
    ], ignore_index=True).drop(columns=["biome_name"])
    merged.to_parquet(HARM / "merged_cellmonth.parquet", index=False)

    yearly = merged.groupby(["year", "method"]).viirs_eq_total.sum().unstack(fill_value=0)
    yearly["total"] = yearly.sum(axis=1)
    print("\nPublished record, global annual totals by source (millions of VIIRS-equivalent detections):")
    print((yearly / 1e6).round(2).to_string())
    step = yearly.total
    print(f"\nContinuity: 2009-2011 mean {step.loc[2009:2011].mean() / 1e6:.2f}M  vs  2012-2014 mean "
          f"{step.loc[2012:2014].mean() / 1e6:.2f}M  ({100 * (step.loc[2012:2014].mean() / step.loc[2009:2011].mean() - 1):+.1f}%)")


# ---------------------------------------------------------------- main

def parse_years(value: str) -> range:
    start, _, end = value.partition("-")
    return range(int(start), int(end or start) + 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", required=True,
                        choices=["features", "sample", "train", "predict", "evaluate", "merge", "all"])
    parser.add_argument("--years", type=parse_years, default=YEARS)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    run = lambda s: args.stage in (s, "all")

    con = connect()
    shared_tables(con)
    if run("features"):
        for year in args.years:
            build_features(con, year, args.force)
    if run("sample"):
        build_samples(con, args.force)
    if run("train"):
        train_models(args.force)
    if run("predict"):
        for year in args.years:
            predict_year(year, "final", args.force)
        for year in range(INNER_VAL[0], INNER_VAL[1] + 1):  # inner preds only for validation years
            if year in args.years:
                predict_year(year, "inner", args.force)
    if run("evaluate"):
        evaluate(con)
    if run("merge"):
        merge_record(con)
    return 0


if __name__ == "__main__":
    sys.exit(main())
