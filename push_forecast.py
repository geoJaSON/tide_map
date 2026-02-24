"""
Push depth forecast images to Supabase.

Runs the full forecast pipeline with multi-station IDW tide
interpolation and Sibul wind setdown, generates a PNG overlay
for each snapshot hour (7 AM, 12 PM, 5 PM) per day, uploads to
Supabase Storage, and upserts metadata to the depth_forecasts table.

Usage:
    python push_forecast.py             # 7-day forecast
    python push_forecast.py --days 3    # 3-day forecast

For daily cron (Windows Task Scheduler or crontab):
    conda run -n geo_env python /path/to/push_forecast.py
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from config import (
    BOUNDS,
    TIDE_STATIONS,
    FETCH_RASTER_PATH,
    SIBUL_AVG_DEPTH_FT,
    WIND_FORECAST_LAT,
    WIND_FORECAST_LON,
    BLUETOPO_DIR,
    CACHE_DIR,
    OUTPUT_DIR,
    MAP_CENTER,
    MAP_ZOOM,
    MIN_NAVIGABLE_DEPTH_FT,
    CAUTION_DEPTH_FT,
    IDW_POWER,
    IDW_COARSE_RES,
    MAX_GRID_DIM,
    SNAPSHOT_HOURS,
)
from src.bathymetry import BathymetryGrid
from src.tides import TideClient
from src.wind import WindForecastClient
from src.setdown import load_fetch_grids, SibulSetdownModel
from src.tide_grid import TideGridBuilder
from src.depth_calc import DepthCalculator
from src.map_builder import MapBuilder, depth_grid_to_png
from src.db import (
    _get_client,
    ensure_bucket,
    upload_image,
    upsert_forecast,
    delete_old_forecasts,
)


def main():
    parser = argparse.ArgumentParser(
        description="Generate depth forecast and push to Supabase"
    )
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument(
        "--local-html", action="store_true",
        help="Also generate local HTML map (like main.py)",
    )
    args = parser.parse_args()

    print("=" * 62)
    print("  Depth Forecast → Supabase")
    print("=" * 62)

    # ------------------------------------------------------------------
    # Step 1: Load bathymetry
    # ------------------------------------------------------------------
    print("\n[1/7] Loading bathymetry...")
    try:
        bathy = BathymetryGrid(BLUETOPO_DIR, BOUNDS, max_dim=MAX_GRID_DIM)
    except FileNotFoundError as e:
        print(f"\n  ERROR: {e}")
        sys.exit(1)

    print(f"  Grid: {bathy.shape[1]} x {bathy.shape[0]} pixels")

    # ------------------------------------------------------------------
    # Step 2: Fetch tide predictions from all stations
    # ------------------------------------------------------------------
    print("\n[2/7] Fetching tide predictions...")
    tides = TideClient(CACHE_DIR)
    now = datetime.now()

    multi_preds = tides.get_multi_station_predictions(
        list(TIDE_STATIONS.keys()), now, num_days=args.days
    )

    if not multi_preds:
        print("  ERROR: Could not fetch tide predictions from any station.")
        sys.exit(1)

    active_stations = {sid: TIDE_STATIONS[sid] for sid in multi_preds}
    print(f"  Active stations: {len(active_stations)} of {len(TIDE_STATIONS)}")
    for sid, info in active_stations.items():
        print(f"    {info['name']} ({sid})")

    # Build spatial tide interpolator
    tide_builder = TideGridBuilder(
        active_stations, bathy.shape, BOUNDS, IDW_POWER, IDW_COARSE_RES
    )

    # ------------------------------------------------------------------
    # Step 2b: Apply VDatum offsets
    # ------------------------------------------------------------------
    print("\n[2b/7] Building spatial VDatum offset grid...")
    vdatum_offsets = {sid: info["vdatum_offset"] for sid, info in active_stations.items()}
    vdatum_grid = tide_builder.build_tide_grid(vdatum_offsets)
    static_depth = bathy.get_static_depth_mllw_ft(vdatum_grid)

    # ------------------------------------------------------------------
    # Step 3: Load fetch raster
    # ------------------------------------------------------------------
    print("\n[3/7] Loading fetch raster...")
    fetch_path = Path(FETCH_RASTER_PATH)
    if not fetch_path.exists():
        print(f"  ERROR: Fetch raster not found: {FETCH_RASTER_PATH}")
        sys.exit(1)

    fetch_grids = load_fetch_grids(str(fetch_path), BOUNDS, bathy.shape)
    setdown_model = SibulSetdownModel(fetch_grids, avg_depth_ft=SIBUL_AVG_DEPTH_FT)

    # ------------------------------------------------------------------
    # Step 4: Fetch wind forecast
    # ------------------------------------------------------------------
    print("\n[4/7] Fetching wind forecast...")
    wind_client = WindForecastClient()
    wind_forecast = wind_client.get_hourly_wind_forecast(
        WIND_FORECAST_LAT, WIND_FORECAST_LON
    )
    if not wind_forecast:
        print("  WARNING: No wind data. Using tides only.")
        first_preds = next(iter(multi_preds.values()))
        wind_forecast = [
            {"time": t["time"], "wind_speed_mph": 0.0,
             "wind_direction": "N", "wind_direction_deg": 0.0}
            for t in first_preds
        ]
    else:
        print(f"  Got {len(wind_forecast)} hourly forecasts")

    # ------------------------------------------------------------------
    # Step 5: Compute snapshot depth grids
    # ------------------------------------------------------------------
    hours_label = ", ".join(f"{h}:00" for h in SNAPSHOT_HOURS)
    print(f"\n[5/7] Computing depth snapshots ({hours_label})...")

    calculator = DepthCalculator(
        static_depth,
        tide_grid_builder=tide_builder,
        setdown_model=setdown_model,
    )
    target_dates = [now + timedelta(days=d) for d in range(args.days)]
    snapshots = calculator.compute_snapshots(
        multi_preds, wind_forecast, target_dates, snapshot_hours=SNAPSHOT_HOURS
    )

    if not snapshots:
        print("  ERROR: No snapshots could be computed.")
        sys.exit(1)

    n_dates = len(set(s["date"] for s in snapshots))
    print(f"  Generated {len(snapshots)} snapshots across {n_dates} day(s)")

    # ------------------------------------------------------------------
    # Step 6: Upload to Supabase
    # ------------------------------------------------------------------
    print("\n[6/7] Uploading to Supabase...")
    client = _get_client()
    ensure_bucket(client)

    bounds_dict = {
        "south": BOUNDS["south"],
        "west": BOUNDS["west"],
        "north": BOUNDS["north"],
        "east": BOUNDS["east"],
    }

    for snap in snapshots:
        d = snap["date"]
        h = snap["hour"]
        eff_level = snap["tide_ft"] + snap["setdown_ft"]

        # Generate PNG
        png_bytes = depth_grid_to_png(
            snap["depth_grid"],
            no_go=MIN_NAVIGABLE_DEPTH_FT,
            caution=CAUTION_DEPTH_FT,
        )

        # Upload image
        image_url = upload_image(client, d, h, png_bytes)

        # Upsert metadata
        upsert_forecast(
            client,
            forecast_date=d,
            forecast_hour=h,
            image_url=image_url,
            bounds=bounds_dict,
            effective_level_ft=eff_level,
            tide_ft=snap["tide_ft"],
            setdown_ft=snap["setdown_ft"],
        )

        time_label = snap["time"].strftime("%I %p").lstrip("0")
        print(f"  {d.strftime('%a %m/%d')} {time_label}: eff {eff_level:+.1f} ft → uploaded")

    # ------------------------------------------------------------------
    # Step 7: Cleanup old data
    # ------------------------------------------------------------------
    print("\n[7/7] Cleaning up...")
    delete_old_forecasts(client, keep_days=14)

    # ------------------------------------------------------------------
    # Optional: also generate local HTML
    # ------------------------------------------------------------------
    if args.local_html:
        print("\n  Generating local HTML map...")
        grid_bounds = bathy.get_grid_bounds_latlon()
        builder = MapBuilder(MAP_CENTER, MAP_ZOOM)
        m = builder.build_snapshot_map(snapshots, grid_bounds)
        Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
        out_path = Path(OUTPUT_DIR) / f"depth_forecast_{now.strftime('%Y%m%d')}.html"
        builder.save(m, str(out_path))
        print(f"  Saved: {out_path}")

    # Summary
    print(f"\n{'=' * 62}")
    print(f"  {'Date':<12} {'Hour':>6} {'Tide':>8} {'Wind':>8} {'Eff':>8}")
    print(f"  {'-' * 50}")
    for s in snapshots:
        eff = s["tide_ft"] + s["setdown_ft"]
        time_label = s["time"].strftime("%I %p").lstrip("0")
        print(
            f"  {s['date'].strftime('%a %m/%d'):<12}"
            f"{time_label:>6}"
            f"{s['tide_ft']:>+7.1f}'"
            f"{s['setdown_ft']:>+7.1f}'"
            f"{eff:>+7.1f}'"
        )
    print(f"{'=' * 62}\n")


if __name__ == "__main__":
    main()
