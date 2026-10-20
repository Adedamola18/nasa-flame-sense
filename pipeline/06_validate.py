"""Validate the harmonised record against independent national burned-area records.

Truth (mapped from Landsat/Sentinel-2 imagery and agency perimeters, independent of MODIS/VIIRS):
  NBAC       Canada  burned area, all fires
  MTBS       USA     large fires only (incl. prescribed burns); 2024-2025 still being mapped
  MapBiomas  Brazil  burned area, all fires

Series (VIIRS-equivalent fire detections per cell-year). All three composites share the SAME
post-2012 series (observed VIIRS with outage days filled), so they differ only before 2012:
  raw_composite  raw Aqua MODIS 2003-2011, then observed VIIRS     (no harmonisation)
  qm_composite   quantile-mapped Aqua MODIS 2003-2011, then observed VIIRS
  record         ML-harmonised Aqua MODIS 2003-2011, then observed VIIRS   (the published record)
  aqua_control   Aqua MODIS in every year: no sensor change at 2012 (constant-sensor control)

Years: 2003-2011 vs 2012-end. 2001-2002 are excluded because the record there is Terra-based.

Checks:
  1. Step test on ln(detections / burned ha), nationally and within sub-regions:
     a. difference of period means (year bootstrap)
     b. segmented regression with a common trend + step (year bootstrap)
     c. step relative to Aqua (TRUTH-FREE): burned area cancels, so this is the step in
        ln(series / Aqua MODIS). Aqua crosses 2012 with no sensor change, so this tests whether a
        series stays continuous with an unchanged sensor. It is a continuity test, not validation
        against the burned-area records; it is reported once per country.
     d. placebo breaks (years with no sensor change), on both the truth-based and the Aqua-relative
        statistics, to show how large steps get by chance. Intervals: year bootstrap and OLS t.
  2. Interannual agreement: Pearson r of ln D vs ln B across years, per period.
  3. Spatial agreement: Spearman rho per year. Primary: burned cells only (independent of every
     product). Secondary: a symmetric mask (cells with burned area or a value in ANY product being
     compared). Matched year windows for ML vs observed (outage-filled) VIIRS. Paired intervals on
     differences; windows of fewer than 6 years use t intervals and are descriptive only.
  4. Global continuity around 2012, with the Aqua control and normal 3-year variability as reference.
  5. ML estimates vs observed VIIRS (national annual totals, 2012-2024; held out from 2021).

Outputs: data/validation/validation.json, data/validation/charts/*.png
"""

import json
import sys
from pathlib import Path

import duckdb
import matplotlib
import numpy as np
import pandas as pd
from scipy import stats

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HARM = ROOT / "data" / "harmonized"
TRUTH = ROOT / "data" / "truth"
REGIONS = ROOT / "data" / "regions" / "cells.parquet"
OUT = ROOT / "data" / "validation"

VIIRS_ERA = 2012
PRE_START = 2003  # first full year in which every method uses Aqua
HELD_OUT_START = 2021
SOURCES = {
    "nbac": {"country": "CAN", "label": "Canada (NBAC)", "end": 2024, "subregion": "ca_ecozone"},
    "mtbs": {"country": "USA", "label": "USA (MTBS)", "end": 2023, "subregion": "biome_name"},
    "mapbiomas": {"country": "BRA", "label": "Brazil (MapBiomas)", "end": 2024, "subregion": "biome_name"},
}
SERIES = ["raw_composite", "qm_composite", "record"]
CONTROL = "aqua_control"
N_BOOT = 2000
MIN_REGION_YEARS = 15


# ---------------------------------------------------------------- data

def cell_years() -> pd.DataFrame:
    df = duckdb.sql(f"""
        WITH base AS (
            SELECT year, row_idx, col_idx,
                   sum(modis_n) FILTER (WHERE sensor = 'aqua') AS aqua, sum(viirs_eq_total) AS qm
            FROM '{HARM / 'baseline_cellmonth.parquet'}' GROUP BY ALL
        ), ml AS (
            SELECT year, row_idx, col_idx, sum(viirs_eq_total) AS ml_est FROM '{HARM / 'ml_cellmonth.parquet'}' GROUP BY ALL
        ), rec AS (
            SELECT year, row_idx, col_idx, sum(viirs_eq_total) AS record, sum(viirs_obs) AS snpp_obs
            FROM '{HARM / 'merged_cellmonth.parquet'}' GROUP BY ALL
        )
        SELECT year, row_idx::BIGINT AS row_idx, col_idx::BIGINT AS col_idx,
               coalesce(aqua, 0) AS aqua, coalesce(qm, 0) AS qm, coalesce(ml_est, 0) AS ml_est,
               coalesce(record, 0) AS record, coalesce(snpp_obs, 0) AS snpp_obs
        FROM rec FULL OUTER JOIN base USING (year, row_idx, col_idx) FULL OUTER JOIN ml USING (year, row_idx, col_idx)
        WHERE year BETWEEN {PRE_START} AND 2024
    """).df()
    post = df.year >= VIIRS_ERA
    df["raw_composite"] = np.where(post, df.record, df.aqua)
    df["qm_composite"] = np.where(post, df.record, df.qm)
    df[CONTROL] = df.aqua
    regions = pd.read_parquet(REGIONS)[["row_idx", "col_idx", "country_iso3", "biome_name", "ca_ecozone"]]
    return df.merge(regions, on=["row_idx", "col_idx"], how="left")


def truth(source: str) -> pd.DataFrame:
    t = pd.read_parquet(TRUTH / f"{source}_cells.parquet")
    t = t[t.year.between(PRE_START, SOURCES[source]["end"])].astype({"row_idx": "int64", "col_idx": "int64"})
    regions = pd.read_parquet(REGIONS)[["row_idx", "col_idx", "country_iso3", "biome_name", "ca_ecozone"]]
    t = t.merge(regions, on=["row_idx", "col_idx"], how="left")
    return t[t.country_iso3 == SOURCES[source]["country"]]


# ---------------------------------------------------------------- 1. step tests

def step_estimates(L: pd.DataFrame, break_year: int = VIIRS_ERA) -> dict:
    """L: index = year, columns = series (log ratios). Returns mean-difference and trend+step estimates."""
    years = L.index.to_numpy()
    post = (years >= break_year).astype(float)
    X = np.column_stack([np.ones_like(post), years - break_year, post])
    mean_diff = L[post == 1].mean() - L[post == 0].mean()
    coef = np.linalg.lstsq(X, L.to_numpy(), rcond=None)[0][2]
    return {"mean_diff": mean_diff.to_numpy(), "trend_step": coef}


def bootstrap_steps(L: pd.DataFrame, rng: np.random.Generator) -> dict:
    """Year bootstrap within each period; the same draws are used for every series so that
    control-adjusted differences (series step minus control step) are paired."""
    pre_y = L.index[L.index < VIIRS_ERA].to_numpy()
    post_y = L.index[L.index >= VIIRS_ERA].to_numpy()
    point = step_estimates(L)
    draws = {"mean_diff": [], "trend_step": []}
    for _ in range(N_BOOT):
        sample = np.concatenate([rng.choice(pre_y, len(pre_y)), rng.choice(post_y, len(post_y))])
        Lb = L.loc[sample]
        if Lb.index.nunique() < 4:
            continue
        est = step_estimates(Lb)
        for k in draws:
            draws[k].append(est[k])
    out = {}
    cols = list(L.columns)
    ci = lambda a: [round(float(np.exp(x)), 3) for x in np.percentile(a, [2.5, 97.5])]
    for kind in ("mean_diff", "trend_step"):
        arr = np.array(draws[kind])
        block = {}
        for i, s in enumerate(cols):
            block[s] = {"ratio": round(float(np.exp(point[kind][i])), 3), "ci95": ci(arr[:, i])}
            if kind == "trend_step":
                block[s]["ci95_t"] = ols_step_ci(L[s])
        if CONTROL in cols:
            c = cols.index(CONTROL)
            for i, s in enumerate(cols):
                if s != CONTROL:
                    block[s]["step_vs_aqua_ratio"] = round(float(np.exp(point[kind][i] - point[kind][c])), 3)
                    block[s]["step_vs_aqua_ci95"] = ci(arr[:, i] - arr[:, c])
                    if kind == "trend_step":
                        block[s]["step_vs_aqua_ci95_t"] = ols_step_ci(L[s] - L[CONTROL])
        out[kind] = block
    return out


def ols_step_ci(y: pd.Series, break_year: int = VIIRS_ERA) -> list[float]:
    """95% OLS t interval (n-3 df) on the step coefficient of a trend+step fit, as a ratio."""
    years = y.index.to_numpy()
    X = np.column_stack([np.ones(len(years)), years - break_year, (years >= break_year).astype(float)])
    beta, *_ = np.linalg.lstsq(X, y.to_numpy(), rcond=None)
    resid = y.to_numpy() - X @ beta
    dof = len(years) - 3
    se = np.sqrt(resid @ resid / dof * np.linalg.inv(X.T @ X)[2, 2])
    half = stats.t.ppf(0.975, dof) * se
    return [round(float(np.exp(beta[2] - half)), 3), round(float(np.exp(beta[2] + half)), 3)]


def placebo_summary(L: pd.DataFrame, actual: float) -> dict:
    """Placebo trend+step ratios for one statistic and where the real 2012 value falls among them."""
    vals = {b: v[L.columns[0]] for b, v in placebo_steps(L).items()}
    arr = np.array(list(vals.values()))
    return {"by_break_year": vals, "min": round(float(arr.min()), 3), "max": round(float(arr.max()), 3),
            "actual_2012": actual, "actual_outside_range": bool(actual < arr.min() or actual > arr.max())}


def placebo_steps(L: pd.DataFrame) -> dict:
    """Trend+step estimates at break years with no sensor change: inside 2003-2011 and inside 2012-end."""
    res = {}
    pre = L[L.index < VIIRS_ERA]
    post = L[L.index >= VIIRS_ERA]
    for b in range(2006, 2010):
        res[b] = {s: round(float(np.exp(v)), 3) for s, v in zip(L.columns, step_estimates(pre, b)["trend_step"])}
    for b in range(2015, int(post.index.max()) - 1):
        res[b] = {s: round(float(np.exp(v)), 3) for s, v in zip(L.columns, step_estimates(post, b)["trend_step"])}
    return res


def national_step(annual: pd.DataFrame, rng: np.random.Generator) -> dict:
    L = np.log(annual[SERIES + [CONTROL]].div(annual.burned_ha, axis=0))
    steps = bootstrap_steps(L, rng)
    rel = (L["record"] - L[CONTROL]).to_frame("record_vs_aqua")
    return {**steps,
            "placebo_record_vs_truth": placebo_summary(L[["record"]], steps["trend_step"]["record"]["ratio"]),
            "placebo_record_vs_aqua": placebo_summary(rel, steps["trend_step"]["record"]["step_vs_aqua_ratio"])}


def pooled_step(panel: pd.DataFrame, rng: np.random.Generator) -> dict:
    """Within-region step on a COMMON region set, unweighted and burned-area weighted.
    Region effects are removed by demeaning ln(D/B) within each region."""
    cols = SERIES + [CONTROL]
    p = panel[(panel[cols] > 0).all(axis=1) & (panel.burned_ha > 0)].copy()
    years_per_region = p.groupby("region").year.nunique()
    p = p[p.region.isin(years_per_region[years_per_region >= MIN_REGION_YEARS].index)]
    if p.region.nunique() < 2:
        return {"note": f"fewer than 2 sub-regions with {MIN_REGION_YEARS}+ valid years"}
    for s in cols:
        p[s] = np.log(p[s] / p.burned_ha)
        p[s] = p[s] - p.groupby("region")[s].transform("mean")
    result = {"regions": int(p.region.nunique()), "region_years": int(len(p))}

    def collapse(frame: pd.DataFrame, weighted: bool) -> pd.DataFrame:
        if weighted:
            return frame.groupby("year").apply(lambda g: pd.Series(np.average(g[cols], axis=0, weights=g.burned_ha), index=cols))
        return frame.groupby("year")[cols].mean()

    def rel_step(frame: pd.DataFrame) -> float:
        y = collapse(frame, False)
        return float(np.exp(step_estimates((y["record"] - y[CONTROL]).to_frame())["trend_step"][0]))

    for label, weighted in (("unweighted", False), ("burned_area_weighted", True)):
        y = collapse(p, weighted)
        steps = bootstrap_steps(y, rng)
        rel = (y["record"] - y[CONTROL]).to_frame("record_vs_aqua")
        result[label] = {**steps, "placebo_record_vs_aqua": placebo_summary(
            rel, steps["trend_step"]["record"]["step_vs_aqua_ratio"])}
    n_years = p.year.nunique()
    full = p.groupby("region").year.nunique()
    balanced = p[p.region.isin(full[full == n_years].index)]
    if balanced.region.nunique() >= 2:
        result["balanced_panel"] = {"regions": int(balanced.region.nunique()),
                                    **bootstrap_steps(collapse(balanced, False), rng)}
    loo = {r: round(rel_step(p[p.region != r]), 3) for r in p.region.unique()}
    result["leave_one_region_out_record_vs_aqua"] = {"min": min(loo.values()), "max": max(loo.values()), "by_region_left_out": loo}
    return result


# ---------------------------------------------------------------- 3. spatial agreement

def coarsen(frame: pd.DataFrame, k: int) -> pd.DataFrame:
    if k == 1:
        return frame
    return frame.assign(row_idx=frame.row_idx // k, col_idx=frame.col_idx // k).groupby(
        ["year", "row_idx", "col_idx"], as_index=False).sum(numeric_only=True)


def spatial_table(c: pd.DataFrame, t: pd.DataFrame, k: int) -> pd.DataFrame:
    """Per cell-year: every series and burned area. Symmetric mask: burned area, or a value in ANY product
    compared in that period (pre-2012: Aqua, QM, ML; from 2012: observed VIIRS, ML), so no product's
    footprint decides which cells are scored."""
    cols = ["aqua", "qm", "ml_est", "record"]
    d = coarsen(c[["year", "row_idx", "col_idx"] + cols], k)
    b = coarsen(t[["year", "row_idx", "col_idx", "burned_ha"]], k)
    m = d.merge(b, on=["year", "row_idx", "col_idx"], how="outer").fillna(0)
    post = m.year >= VIIRS_ERA
    detected = np.where(post, (m.record > 0) | (m.ml_est > 0), (m.aqua > 0) | (m.qm > 0) | (m.ml_est > 0))
    return m[(m.burned_ha > 0) | detected]


def yearly_rho(m: pd.DataFrame, value: str, years, burned_only: bool = False) -> dict[int, float]:
    out = {}
    for y in years:
        g = m[m.year == y]
        if burned_only:
            g = g[g.burned_ha > 0]
        if len(g) >= 50:
            out[y] = stats.spearmanr(g[value], g.burned_ha).statistic
    return out


def paired_diff(a: dict, b: dict, rng: np.random.Generator) -> dict:
    """Mean paired difference over years. Fewer than 6 years: t interval, marked descriptive."""
    years = sorted(set(a) & set(b))
    d = np.array([a[y] - b[y] for y in years])
    out = {"mean_diff": round(float(d.mean()), 3), "years": len(years), "yearly": {int(y): round(float(a[y] - b[y]), 3) for y in years}}
    if len(d) < 6:
        lo, hi = stats.t.interval(0.95, len(d) - 1, d.mean(), stats.sem(d)) if len(d) > 1 else (np.nan, np.nan)
        out.update({"ci95_t": [round(float(lo), 3), round(float(hi), 3)], "descriptive_only": True})
    else:
        boots = rng.choice(d, (N_BOOT, len(d))).mean(1)
        out["ci95"] = [round(float(x), 3) for x in np.percentile(boots, [2.5, 97.5])]
    return out


def spatial_agreement(c: pd.DataFrame, t: pd.DataFrame, end: int, rng: np.random.Generator) -> dict:
    pre = range(PRE_START, VIIRS_ERA)
    in_sample = range(VIIRS_ERA, min(end, HELD_OUT_START - 1) + 1)
    held_out = range(HELD_OUT_START, end + 1)
    result = {}
    for scale, k in (("0.25deg", 1), ("1deg", 4)):
        m = spatial_table(c, t, k)
        for variant, burned_only in (("burned_cells_only", True), ("symmetric_mask", False)):
            mean = lambda r: round(float(np.mean(list(r.values()))), 3) if r else None
            pre_r = {s: yearly_rho(m, col, pre, burned_only) for s, col in (("raw_modis", "aqua"), ("qm", "qm"), ("ml", "ml_est"))}
            win = {}
            for wname, yrs in (("2012_2020_ml_in_sample", in_sample), ("2021_on_ml_held_out", held_out)):
                if len(yrs) == 0:
                    continue
                v = yearly_rho(m, "record", yrs, burned_only)  # observed VIIRS, outage days filled
                ml = yearly_rho(m, "ml_est", yrs, burned_only)
                win[wname] = {"viirs_observed": mean(v), "ml": mean(ml), "ml_minus_viirs": paired_diff(ml, v, rng)}
            result[f"{scale}/{variant}"] = {
                "pre_2003_2011": {s: mean(r) for s, r in pre_r.items()},
                "pre_ml_minus_raw": paired_diff(pre_r["ml"], pre_r["raw_modis"], rng),
                "pre_ml_minus_qm": paired_diff(pre_r["ml"], pre_r["qm"], rng),
                "post": win,
                "cells_per_year_median": int((m[m.burned_ha > 0] if burned_only else m).groupby("year").size().median()),
            }
    return result


# ---------------------------------------------------------------- per source

def validate_source(cy: pd.DataFrame, source: str, rng: np.random.Generator) -> dict:
    cfg = SOURCES[source]
    t = truth(source)
    c = cy[(cy.country_iso3 == cfg["country"]) & cy.year.between(PRE_START, cfg["end"])]
    cols = SERIES + [CONTROL]
    annual = c.groupby("year")[cols].sum().join(t.groupby("year").burned_ha.sum(), how="inner")
    result = {"label": cfg["label"], "years": [PRE_START, cfg["end"]],
              "annual": annual.round(1).reset_index().to_dict(orient="list")}

    result["step_national"] = national_step(annual, rng)

    # Robustness: only cells that burned at least once in the truth record (the truth footprint).
    footprint = t.groupby(["row_idx", "col_idx"]).burned_ha.sum().reset_index()[["row_idx", "col_idx"]]
    cf = c.merge(footprint, on=["row_idx", "col_idx"])
    annual_fp = cf.groupby("year")[cols].sum().join(t.groupby("year").burned_ha.sum(), how="inner")
    result["step_national_truth_footprint"] = {
        "share_of_detections_in_footprint": round(float(annual_fp.record.sum() / annual.record.sum()), 3),
        **bootstrap_steps(np.log(annual_fp[cols].div(annual_fp.burned_ha, axis=0)), rng)}

    sub = cfg["subregion"]
    det = c.groupby([sub, "year"])[cols].sum().reset_index().rename(columns={sub: "region"})
    ba = t.groupby([sub, "year"]).burned_ha.sum().reset_index().rename(columns={sub: "region"})
    result["step_subregional"] = {"by": sub, **pooled_step(det.merge(ba, on=["region", "year"]), rng)}

    pre = annual[annual.index < VIIRS_ERA]
    post = annual[annual.index >= VIIRS_ERA]
    r = lambda a, b: round(float(stats.pearsonr(np.log(a), np.log(b)).statistic), 3)
    result["interannual_r_log"] = {
        "note": "pre and post periods have different burned-area spread, so compare methods within a period, not across",
        "pre_raw_modis": r(pre.raw_composite, pre.burned_ha), "pre_qm": r(pre.qm_composite, pre.burned_ha),
        "pre_record_ml": r(pre.record, pre.burned_ha), "post_viirs_observed": r(post.record, post.burned_ha),
        "burned_area_log_sd": {"pre": round(float(np.log(pre.burned_ha).std()), 3), "post": round(float(np.log(post.burned_ha).std()), 3)},
    }

    result["spatial_rho"] = spatial_agreement(c, t, cfg["end"], rng)

    ml_vs_obs = c[c.year >= VIIRS_ERA].groupby("year")[["ml_est", "record"]].sum()
    result["ml_vs_observed_viirs_pct"] = {
        int(y): {"bias_pct": round(float(100 * (r_.ml_est / r_.record - 1)), 1), "held_out": bool(y >= HELD_OUT_START)}
        for y, r_ in ml_vs_obs.iterrows()}
    return result


def global_continuity(cy: pd.DataFrame) -> dict:
    g = cy.groupby("year")[SERIES + [CONTROL]].sum()
    step = lambda s: round(float(100 * (g.loc[2012:2014, s].mean() / g.loc[2009:2011, s].mean() - 1)), 1)
    a = g[CONTROL]
    windows = [100 * (a.loc[y + 3:y + 5].mean() / a.loc[y:y + 2].mean() - 1) for y in range(PRE_START, int(a.index.max()) - 4)]
    return {
        "annual_millions": (g / 1e6).round(3).reset_index().to_dict(orient="list"),
        "step_2009_2011_to_2012_2014_pct": {s: step(s) for s in g.columns},
        "aqua_control_3yr_to_3yr_change_pct": {"min": round(float(min(windows)), 1), "max": round(float(max(windows)), 1),
                                               "sd": round(float(np.std(windows)), 1), "n_windows": len(windows)},
    }


def charts(results: dict) -> None:
    (OUT / "charts").mkdir(parents=True, exist_ok=True)
    style = {"raw_composite": ("#9aa0a6", "Raw (Aqua MODIS, then VIIRS)"),
             "qm_composite": ("#e8a33d", "Quantile mapping"),
             "record": ("#c2410c", "Published record (ML)"),
             CONTROL: ("#2563eb", "Aqua MODIS only (no sensor change)")}
    for source, r in results["sources"].items():
        a = pd.DataFrame(r["annual"]).set_index("year")
        fig, ax = plt.subplots(figsize=(8, 4))
        for s, (col, name) in style.items():
            ax.plot(a.index, a[s] / a.burned_ha * 1000, marker="o", ms=3, color=col, label=name,
                    ls="--" if s == CONTROL else "-")
        ax.axvline(VIIRS_ERA - 0.5, color="#555", ls=":", lw=1)
        ax.set_yscale("log")
        ax.set_ylabel("Fire detections per 1,000 ha burned")
        ax.set_title(f"{r['label']}: detections per burned hectare, {PRE_START}-{a.index.max()}")
        ax.legend(fontsize=7)
        fig.tight_layout()
        fig.savefig(OUT / "charts" / f"step_{source}.png", dpi=150)
        plt.close(fig)
    g = pd.DataFrame(results["global"]["annual_millions"]).set_index("year")
    fig, ax = plt.subplots(figsize=(8, 4))
    for s, (col, name) in style.items():
        ax.plot(g.index, g[s], marker="o", ms=3, color=col, label=name, ls="--" if s == CONTROL else "-")
    ax.axvline(VIIRS_ERA - 0.5, color="#555", ls=":", lw=1)
    ax.set_ylabel("Global fire detections (millions)")
    ax.set_title(f"Global annual fire detections, {PRE_START}-2024")
    ax.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(OUT / "charts" / "global_continuity.png", dpi=150)
    plt.close(fig)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(42)
    cy = cell_years()
    print(f"{len(cy):,} cell-years loaded ({PRE_START}-2024)")
    results = {
        "notes": {
            "known_gap": "No uncertainty intervals on the published record yet (planned: conformal intervals on ML cell-months).",
            "aqua_control": "Aqua MODIS has no sensor change at 2012; its step reflects truth-data and real-world change. "
                            "Its late-mission orbit drift (2022-2024) is part of the control as observed.",
        },
        "sources": {s: validate_source(cy, s, rng) for s in SOURCES},
        "global": global_continuity(cy),
    }
    (OUT / "validation.json").write_text(json.dumps(results, indent=2, default=float))
    charts(results)
    report(results)
    return 0


def report(results: dict) -> None:
    for source, r in results["sources"].items():
        print(f"\n=== {r['label']} {r['years']}")
        nat = r["step_national"]["trend_step"]
        print("  TRUTH-BASED step, trend+step [ratio, bootstrap CI, t CI]")
        for s in SERIES + [CONTROL]:
            v = nat[s]
            print(f"    {s:<14} {v['ratio']:>7} {v['ci95']} t{v['ci95_t']}")
        fp = r["step_national_truth_footprint"]["trend_step"]["record"]
        print(f"    record, truth footprint only: {fp['ratio']} t{fp['ci95_t']}  "
              f"(share of detections in footprint {r['step_national_truth_footprint']['share_of_detections_in_footprint']})")
        pl = r["step_national"]["placebo_record_vs_truth"]
        print(f"    placebo (record vs truth) range {pl['min']}..{pl['max']}; 2012 = {pl['actual_2012']}, outside range: {pl['actual_outside_range']}")
        print("  TRUTH-FREE continuity: step in ln(series / Aqua MODIS)")
        for s in SERIES:
            v = nat[s]
            print(f"    {s:<14} {v['step_vs_aqua_ratio']:>7} {v['step_vs_aqua_ci95']} t{v['step_vs_aqua_ci95_t']}")
        pa = r["step_national"]["placebo_record_vs_aqua"]
        print(f"    placebo (record vs Aqua) range {pa['min']}..{pa['max']}; 2012 = {pa['actual_2012']}, outside range: {pa['actual_outside_range']}")
        sub = r["step_subregional"]
        if "unweighted" in sub:
            print(f"  within {sub['by']} ({sub['regions']} regions), record vs Aqua [trend+step]:")
            for w in ("unweighted", "burned_area_weighted"):
                v = sub[w]["trend_step"]["record"]
                pl = sub[w]["placebo_record_vs_aqua"]
                print(f"    {w:<22} {v['step_vs_aqua_ratio']:>7} t{v['step_vs_aqua_ci95_t']}  placebo {pl['min']}..{pl['max']} outside: {pl['actual_outside_range']}")
            if "balanced_panel" in sub:
                v = sub["balanced_panel"]["trend_step"]["record"]
                print(f"    balanced panel ({sub['balanced_panel']['regions']} regions)  {v['step_vs_aqua_ratio']:>7} t{v['step_vs_aqua_ci95_t']}")
            lo = sub["leave_one_region_out_record_vs_aqua"]
            print(f"    leave-one-region-out range {lo['min']}..{lo['max']}")
        print("  interannual r:", {k: v for k, v in r["interannual_r_log"].items() if k != "note"})
        for key in ("0.25deg/burned_cells_only", "1deg/burned_cells_only", "0.25deg/symmetric_mask", "1deg/symmetric_mask"):
            sp = r["spatial_rho"][key]
            d = sp["pre_ml_minus_raw"]
            print(f"  rho {key} (cells/yr {sp['cells_per_year_median']}): pre {sp['pre_2003_2011']}  ML-raw {d['mean_diff']} {d.get('ci95', d.get('ci95_t'))}")
            for w, v in sp["post"].items():
                dd = v["ml_minus_viirs"]
                print(f"      {w}: VIIRS {v['viirs_observed']} ML {v['ml']} diff {dd['mean_diff']} {dd.get('ci95', dd.get('ci95_t'))}"
                      + (" (descriptive)" if dd.get("descriptive_only") else ""))
        print("  ML vs observed VIIRS (% bias, * held out): " + ", ".join(
            f"{y}{'*' if v['held_out'] else ''}:{v['bias_pct']}" for y, v in r["ml_vs_observed_viirs_pct"].items()))
    g = results["global"]
    print(f"\nGlobal step 2009-11 -> 2012-14 (%): {g['step_2009_2011_to_2012_2014_pct']}")
    print(f"Aqua control, normal 3-yr to 3-yr change (%): {g['aqua_control_3yr_to_3yr_change_pct']}")


if __name__ == "__main__":
    sys.exit(main())
