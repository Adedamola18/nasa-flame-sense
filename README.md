# The Long Watch

25 years of the planet's fires, told by the satellites that watched them, and stitched into one continuous global record as those satellites retire. Canada is the featured case.

Built for the NASA Space Apps Challenge 2026, challenge **"Harmonization of MODIS and VIIRS Hot Spots"**.

## The problem

The global satellite fire record comes from two instrument families. MODIS (Terra, Aqua) has 1 km pixels; VIIRS (Suomi-NPP, NOAA-20, NOAA-21) has 375 m pixels and detects far more, smaller fires. Raw counts jump about sixfold when VIIRS arrives in 2012, so any trend that crosses 2012 is distorted, everywhere on Earth. Meanwhile both MODIS satellites are drifting and winding down.

## What this does

1. **Grids** every FIRMS detection (MODIS C6.1 2001–2024, VIIRS S-NPP 2012–2024, NOAA-20 2018–2024) onto 0.25° cells per local solar day.
2. **Harmonises** MODIS into VIIRS-equivalent counts, with two methods compared on the same hold-out scoreboard:
   - a statistical baseline: quantile mapping per biome and season;
   - a LightGBM model (Tweedie objective, monotone constraints, no year feature) that predicts what Suomi-NPP would have seen from MODIS observations and context, including fires MODIS missed.
3. **Publishes one record:** Terra mapping (to June 2002), ML-harmonised Aqua MODIS (July 2002–2011), then observed VIIRS (2012–2024) with S-NPP outage days filled by NOAA-20 or the model.
4. **Validates** it against independent national burned-area records: NBAC (Canada), MTBS (USA) and MapBiomas Fogo (Brazil), including a test for a remaining step at 2012.

## Pipeline

| Script | Output |
|---|---|
| `pipeline/01_fetch_firms.py` | FIRMS yearly archives (global or Canada), resumable, with a SHA-256 manifest |
| `pipeline/02_grid.py` | 0.25° daily grid per sensor (`data/grid/`) |
| `pipeline/03_regions.py` | Biome, ecoregion, country and Canadian ecozone per cell (`data/regions/cells.parquet`) |
| `pipeline/04_fetch_truth.py` | NBAC, MTBS and MapBiomas burned area per cell-year (`data/truth/`) |
| `pipeline/05_harmonize_baseline.py` | Quantile-mapping baseline and hold-out metrics |
| `pipeline/05b_harmonize_ml.py` | ML features, sampling, training, prediction, scoreboard and the merged record |
| `pipeline/06_validate.py` | Validation against the national records (`data/validation/`) |

Requires Python 3.11+ (developed on 3.14).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # LightGBM on macOS also needs: brew install libomp
python pipeline/01_fetch_firms.py --scope global
python pipeline/02_grid.py --scope global
python pipeline/03_regions.py
python pipeline/04_fetch_truth.py
python pipeline/05_harmonize_baseline.py
python pipeline/05b_harmonize_ml.py --stage all
python pipeline/06_validate.py
```

The raw archives are about 10 GB and the intermediate feature and prediction files about 25 GB, so they are not in git. Small outputs (region lookups, burned-area summaries, metrics, validation results) are committed.

## Repository layout

```
pipeline/   Python data pipeline (scripts run in order)
data/       Small committed outputs; large data is rebuilt locally
web/        Next.js story and explorer (Cloudflare)
worker/     Cloudflare Worker config
```

## Data sources

| Dataset | Provider | Licence |
|---|---|---|
| FIRMS MODIS C6.1 and VIIRS 375 m active fire (S-NPP, NOAA-20) | NASA LANCE / FIRMS | NASA open data, acknowledgement requested |
| NBAC, National Burned Area Composite (release 2026-05-13) | Natural Resources Canada | Open Government Licence – Canada |
| MTBS burned-area perimeters | USGS and USDA Forest Service | US public domain |
| MapBiomas Fire (Fogo), Collection 5.1 | MapBiomas | CC BY 4.0 |
| RESOLVE Ecoregions 2017 (Dinerstein et al. 2017) | RESOLVE | CC BY 4.0 |
| Admin 0 countries, 1:50m | Natural Earth | Public domain |
| Ecozones, Ecological Framework of Canada | Agriculture and Agri-Food Canada | Open Government Licence – Canada |

The NBAC download URL is pinned to the 2026-05-13 release for reproducibility; if NRCan withdraws that release, update `NBAC_URL` in `pipeline/04_fetch_truth.py`.

## Licence

Apache License 2.0. See `LICENSE`. Third-party datasets keep their own licences and citations.
