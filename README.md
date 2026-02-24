# South Louisiana Navigable Depth Predictor

Predicts where shallow-draft boats can safely operate across coastal Louisiana by combining NOAA bathymetry, multi-station tide forecasts, and wind-driven water level changes.

Built for oyster lease surveying — boats typically need 2+ ft of depth, and a strong north wind can push water out of the bays fast enough to strand you.

## How It Works

### Data Sources

| Source | What it provides | API |
|--------|-----------------|-----|
| **BlueTopo** (NOAA) | Seafloor elevation at ~1 m resolution (NAVD88, meters) | Local GeoTIFF tiles |
| **CO-OPS Tide Predictions** | Hourly predicted water levels at 5 stations (MLLW, feet) | `tidesandcurrents.noaa.gov` |
| **NWS Wind Forecast** | Hourly wind speed & direction, 7 days out | `api.weather.gov` |

### Depth Calculation

For every pixel in the grid, at each snapshot hour (7 AM, 12 PM, 5 PM):

```
predicted_depth = static_depth + tide_height + wind_setdown
```

Where:

- **static_depth** — depth of water at Mean Lower Low Water (MLLW), converted from BlueTopo:
  ```
  elev_ft = elevation_navd88_m × 3.28084
  static_depth = -(elev_ft + vdatum_offset)
  ```
  The NAVD88-to-MLLW offset is spatially interpolated using NOAA VDatum values provided per tide station (ranging from -0.63 ft at Port Fourchon to -0.35 ft at Shell Beach) so pixel depths smoothly adjust to local tidal dynamics.

- **tide_height** — spatially interpolated from 5 NOAA stations using Inverse Distance Weighting (IDW, power=2). Each pixel gets a distance-weighted blend rather than a single uniform value.

- **wind_setdown** — spatially varying, computed using the **USACE wind stress formula** with a linear tilt model:
  ```
  S = Cd × U² × F / (g × d)
  ```
  Where Cd = surface drag coefficient (2.5×10⁻⁶), U = wind speed (ft/s), F = fetch distance (ft), g = 32.174 ft/s², d = average basin depth (ft).

  A **4-band fetch raster** (N, E, S, W) provides the fetch distance at each pixel, and a linear tilt distributes the effect: water drops on the upwind shore (setdown) and rises on the downwind shore (setup). This means setdown varies spatially — open bays with long fetch see more effect than sheltered channels.

### Snapshot Forecasts

The tool generates **three snapshots per day** at 7 AM, 12 PM, and 5 PM, showing predicted depth at each time. This lets you see how conditions change throughout the work day — an area may be inaccessible at morning low tide but navigable by the afternoon.

Each snapshot is color-coded:

| Color | Depth | Meaning |
|-------|-------|---------|
| Red | < 2 ft | No-go — too shallow |
| Yellow | 2–4 ft | Caution — tight clearance |
| Green | ≥ 4 ft | Safe |
| Gray | — | No survey data |

### Accuracy Considerations

- **Tide–wind hour alignment**: Tide predictions are hourly from midnight (NOAA); NWS wind can start at the current hour. Hours with tide but no wind use the **nearest available** wind setdown to avoid assuming calm.
- **Timezone**: Tides use NOAA `lst_ldt`; wind uses NWS local time (naive). If NOAA applies no DST to predictions, a 1-hour offset is possible in summer.
- **Wind setdown**: Uses the USACE wind stress formula (Cd=2.5×10⁻⁶) with a configurable average basin depth (default 5 ft). Accuracy depends on the quality of the fetch raster and the representativeness of the average depth. The drag coefficient can be tuned via `WindSetupModel.DRAG_COEFF`.
- **VDatum offsets**: Per-station offsets from NOAA VDatum are IDW-interpolated across the grid. Ground truthing may reveal a need for adjustment.

### Tide Station Coverage

Predictions are fetched from 5 NOAA CO-OPS stations spanning the coverage area:

| Station | ID | Location | VDatum Offset |
|---------|-----|----------|---------------|
| Port Fourchon, Belle Pass | 8762075 | 29.11°N, 90.20°W | -0.63 ft |
| Grand Isle | 8761724 | 29.26°N, 89.96°W | -0.55 ft |
| Pilottown | 8760721 | 29.18°N, 89.26°W | -0.45 ft |
| Pilots Station East, SW Pass | 8760922 | 28.93°N, 89.41°W | -0.40 ft |
| Shell Beach | 8761305 | 29.87°N, 89.67°W | -0.35 ft |

IDW interpolation blends these spatially — pixels near Grand Isle weight that station heavily, while pixels near Shell Beach weight that one. If a station fails, the remaining stations automatically cover the gap.

## Setup

### Prerequisites

- Python 3.10+ with conda (miniforge recommended)
- BlueTopo GeoTIFF tiles for your area (download from NOAA)
- Fetch raster — 4-band GeoTIFF with cardinal fetch distances (see below)
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

### Prepare Fetch Raster

Create a 4-band GeoTIFF at `data/fetch/fetch_4dir.tif`:
- **CRS**: EPSG:4326
- **Bands**: 1=North, 2=East, 3=South, 4=West
- **Values**: Fetch distance in **feet** (distance from each water pixel to nearest land in that cardinal direction)
- Must cover the project bounds (will be windowed and resampled to match the depth grid)

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

Opens in any browser with toggleable layers per snapshot (3 per day), satellite/street base maps, and a conditions popup.

### Push forecasts to Supabase

```bash
python push_forecast.py                # 7-day, upload to Supabase
python push_forecast.py --local-html   # also save local HTML
```

Uploads a PNG overlay per snapshot hour to Supabase Storage and upserts metadata (date, hour, bounds, effective water level) to the `depth_forecasts` table. Old forecasts (>14 days) are cleaned up automatically.

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
│   ├── setdown.py         # Sibul wind setdown model (spatial)
│   ├── tide_grid.py       # IDW spatial tide interpolation
│   ├── depth_calc.py      # Combines bathy + tides + wind → depth grids
│   ├── map_builder.py     # Folium HTML map + PNG generation
│   └── db.py              # Supabase Storage + table client
├── data/
│   ├── bluetopo/           # BlueTopo tiles + mosaic.vrt
│   ├── fetch/              # Fetch raster (4-band GeoTIFF)
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
  .order('forecast_date, forecast_hour')
```

Each row provides:
- `forecast_hour` — snapshot hour (7, 12, or 17)
- `image_url` — public PNG URL, use directly as a Leaflet/Mapbox `ImageOverlay`
- `bounds` — `{"south", "west", "north", "east"}` for overlay positioning
- `effective_level_ft` — tide + wind setdown at that hour
- `tide_ft`, `setdown_ft` — individual components

## Tuning

Key parameters in `config.py`:

| Parameter | Default | What it controls |
|-----------|---------|-----------------|
| `SIBUL_AVG_DEPTH_FT` | 5.0 | Average basin depth for wind setup formula. Lower = more setdown |
| `FETCH_RASTER_PATH` | `data/fetch/fetch_4dir.tif` | Path to 4-band fetch GeoTIFF |
| `TIDE_STATIONS[].vdatum_offset` | Varies | NAVD88-to-MLLW shift per station (ft). IDW-interpolated across the grid |
| `MIN_NAVIGABLE_DEPTH_FT` | 2.0 | Red threshold (feet) |
| `CAUTION_DEPTH_FT` | 4.0 | Yellow threshold (feet) |
| `SNAPSHOT_HOURS` | [7, 12, 17] | Hours of day to generate depth maps |
| `MAX_GRID_DIM` | 5000 | Cap on grid resolution. Higher = more detail, more memory |
| `IDW_POWER` | 2 | IDW exponent. Higher = closer stations dominate more |
