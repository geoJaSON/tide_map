# South Louisiana Navigable Depth Predictor

Predicts where shallow-draft boats can safely operate across coastal Louisiana by combining NOAA bathymetry, multi-station tide forecasts, and wind-driven water level changes.

Built for oyster lease surveying — boats typically need 3 ft of depth, and a strong north wind can push water out of the bays fast enough to strand you.

## How It Works

### Data Sources

| Source | What it provides | API |
|--------|-----------------|-----|
| **BlueTopo** (NOAA) | Seafloor elevation at ~1 m resolution (NAVD88, meters) | Local GeoTIFF tiles |
| **CO-OPS Tide Predictions** | Hourly predicted water levels at 5 stations (MLLW, feet) | `tidesandcurrents.noaa.gov` |
| **NWS Wind Forecast** | Hourly wind speed & direction, 7 days out | `api.weather.gov` |

### Depth Calculation

For every pixel in the grid, at every forecast hour:

```
predicted_depth = static_depth + tide_height + wind_setdown
```

Where:

- **static_depth** — depth of water at Mean Lower Low Water (MLLW), converted from BlueTopo:
  ```
  elev_ft = elevation_navd88_m × 3.28084
  static_depth = -(elev_ft + NAVD88_to_MLLW_offset)
  ```
  The NAVD88-to-MLLW offset is approximately **-0.63 ft** (NAVD88 zero sits ~0.63 ft above MLLW zero along this coast).

- **tide_height** — spatially interpolated from 5 NOAA stations using Inverse Distance Weighting (IDW, power=2). Each pixel gets a distance-weighted blend rather than a single uniform value.

- **wind_setdown** — empirical model for wind-driven water level change:
  ```
  setdown = -0.003 × wind_speed² × cos(wind_direction)
  ```
  A 20 mph north wind produces ~1.2 ft of setdown (water drops). South wind pushes water in (level rises). The coefficient (0.003 ft/mph²) is calibrated for the Louisiana bight.

### Daily Worst-Case Map

Each day's forecast shows the **per-pixel minimum depth** across all 24 hours — the worst-case scenario for navigation. The output is color-coded:

| Color | Depth | Meaning |
|-------|-------|---------|
| Red | < 3 ft | No-go — too shallow |
| Yellow | 3–4 ft | Caution — tight clearance |
| Green | ≥ 4 ft | Safe |
| Gray | — | No survey data |

### Accuracy considerations

- **Tide–wind hour alignment**: Tide predictions are hourly from midnight (NOAA); NWS wind can start at the current hour. Hours with tide but no wind used to be treated as zero setdown; the code now uses the **nearest available** wind setdown for such hours to avoid assuming calm.
- **Worst hour**: The time shown as “worst” is the hour when **average** depth over the grid is lowest, not necessarily when the shallowest pixel is at its minimum.
- **Timezone**: Tides use NOAA `lst_ldt`; wind uses NWS local time (naive). If NOAA applies no DST to predictions, a 1-hour offset is possible in summer; worth verifying against a known event.
- **Wind setdown**: Empirical coefficient (0.003) is unvalidated in-repo; Phase 2 regression on observed vs predicted residuals would improve reliability.

### Tide Station Coverage

Predictions are fetched from 5 NOAA CO-OPS stations spanning the coverage area:

| Station | ID | Location |
|---------|-----|----------|
| Port Fourchon, Belle Pass | 8762075 | 29.11°N, 90.20°W |
| Grand Isle | 8761724 | 29.26°N, 89.96°W |
| Pilottown | 8760721 | 29.18°N, 89.26°W |
| Pilots Station East, SW Pass | 8760922 | 28.93°N, 89.41°W |
| Shell Beach | 8761305 | 29.87°N, 89.67°W |

IDW interpolation blends these spatially — pixels near Grand Isle weight that station heavily, while pixels near Shell Beach weight that one. If a station fails, the remaining stations automatically cover the gap.

## Setup

### Prerequisites

- Python 3.10+ with conda (miniforge recommended)
- BlueTopo GeoTIFF tiles for your area (download from NOAA)
- Supabase project (optional, for web integration)

### Install

```bash
conda create -n geo_env python=3.11
conda activate geo_env
conda install -c conda-forge gdal rasterio pyproj numpy
pip install folium branca pillow requests supabase python-dotenv
```

### Prepare Bathymetry

1. Download BlueTopo tiles from NOAA and place them in `data/bluetopo/Louisiana_Bathy/`
2. Build the VRT mosaic (one-time, re-run when adding tiles):
   ```bash
   python build_vrt.py --src data/bluetopo/Louisiana_Bathy
   ```

### Environment Variables (for Supabase push)

Create a `.env` file:
```
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_KEY=your-service-key
```

Run `schema.sql` in the Supabase SQL Editor to create the `depth_forecasts` table.

## Usage

### Generate local HTML map

```bash
python main.py                         # 7-day forecast
python main.py --days 3                # 3-day forecast
python main.py --output custom.html    # custom filename
```

Opens in any browser with toggleable layers per day, satellite/street base maps, and a conditions popup.

### Push forecasts to Supabase

```bash
python push_forecast.py                # 7-day, upload to Supabase
python push_forecast.py --local-html   # also save local HTML
```

Uploads a PNG overlay per day to Supabase Storage and upserts metadata (date, bounds, effective low water, worst hour) to the `depth_forecasts` table. Old forecasts (>14 days) are cleaned up automatically.

### Daily automation

Windows Task Scheduler or crontab — run each morning:
```bash
conda run -n geo_env python /path/to/push_forecast.py
```

## Project Structure

```
tide_map/
├── config.py              # Bounds, stations, thresholds, paths
├── main.py                # CLI → local HTML map
├── push_forecast.py       # CLI → Supabase upload
├── build_vrt.py           # BlueTopo tile → VRT mosaic
├── schema.sql             # Supabase table DDL
├── requirements.txt
├── .env                   # Supabase credentials (not committed)
├── src/
│   ├── bathymetry.py      # BlueTopo loader (VRT or raw tiles)
│   ├── tides.py           # NOAA CO-OPS API client
│   ├── wind.py            # NWS Weather API client
│   ├── setdown.py         # Wind setdown model
│   ├── tide_grid.py       # IDW spatial tide interpolation
│   ├── depth_calc.py      # Combines bathy + tides + wind → depth grids
│   ├── map_builder.py     # Folium HTML map + PNG generation
│   └── db.py              # Supabase Storage + table client
├── data/
│   ├── bluetopo/           # BlueTopo tiles + mosaic.vrt
│   └── cache/              # Cached API responses (6-hour TTL)
└── output/                 # Generated HTML maps
```

## Using Forecasts in Your Web App

Query Supabase for the current forecast window:

```js
const { data } = await supabase
  .from('depth_forecasts')
  .select('*')
  .gte('forecast_date', '2026-02-23')
  .order('forecast_date')
```

Each row provides:
- `image_url` — public PNG URL, use directly as a Leaflet/Mapbox `ImageOverlay`
- `bounds` — `{"south", "west", "north", "east"}` for overlay positioning
- `effective_low_ft` — worst-case water level for the day
- `worst_hour` — when conditions are worst

## Tuning

Key parameters in `config.py`:

| Parameter | Default | What it controls |
|-----------|---------|-----------------|
| `SETDOWN_COEFF` | 0.003 | ft of setdown per mph². Increase if observed setdown exceeds predictions |
| `NAVD88_TO_MLLW_OFFSET_FT` | -0.63 | Vertical datum shift. Refine with NOAA VDatum for your specific area |
| `MIN_NAVIGABLE_DEPTH_FT` | 3.0 | Red threshold (feet) |
| `CAUTION_DEPTH_FT` | 4.0 | Yellow threshold (feet) |
| `MAX_GRID_DIM` | 5000 | Cap on grid resolution. Higher = more detail, more memory |
| `IDW_POWER` | 2 | IDW exponent. Higher = closer stations dominate more |
