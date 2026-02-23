"""
Push depth forecast images to Supabase.

Runs the full forecast pipeline with multi-station IDW tide
interpolation, generates a PNG overlay for each day, uploads to
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
    NAVD88_TO_MLLW_OFFSET_FT,
    SETDOWN_COEFF,
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
)
from src.bathymetry import BathymetryGrid
from src.tides import TideClient
from src.wind import WindForecastClient
from src.setdown import WindSetdownModel
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
    print("\n[1/6] Loading bathymetry...")
    try:
        bathy = BathymetryGrid(BLUETOPO_DIR, BOUNDS, max_dim=MAX_GRID_DIM)
    except FileNotFoundError as e:
        print(f"\n  ERROR: {e}")
        sys.exit(1)

    static_depth = bathy.get_static_depth_mllw_ft(NAVD88_TO_MLLW_OFFSET_FT)
    print(f"  Grid: {bathy.shape[1]} x {bathy.shape[0]} pixels")

    # ------------------------------------------------------------------
    # Step 2: Fetch tide predictions from all stations
    # ------------------------------------------------------------------
    print("\n[2/6] Fetching tide predictions...")
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
    # Step 3: Fetch wind forecast
    # ------------------------------------------------------------------
    print("\n[3/6] Fetching wind forecast...")
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
    # Step 4: Compute setdown + depth grids
    # ------------------------------------------------------------------
    print("\n[4/6] Computing depth grids...")
    setdown_model = WindSetdownModel(coeff=SETDOWN_COEFF)
    setdown_series = setdown_model.calculate_setdown_timeseries(wind_forecast)

    calculator = DepthCalculator(static_depth, tide_grid_builder=tide_builder)
    target_dates = [now + timedelta(days=d) for d in range(args.days)]
    summaries = calculator.compute_daily_summaries(
        multi_preds, setdown_series, target_dates
    )

    if not summaries:
        print("  ERROR: No daily summaries could be computed.")
        sys.exit(1)

    print(f"  Generated {len(summaries)} daily forecast(s)")

    # ------------------------------------------------------------------
    # Step 5: Upload to Supabase
    # ------------------------------------------------------------------
    print("\n[5/6] Uploading to Supabase...")
    client = _get_client()
    ensure_bucket(client)

    bounds_dict = {
        "south": BOUNDS["south"],
        "west": BOUNDS["west"],
        "north": BOUNDS["north"],
        "east": BOUNDS["east"],
    }

    for summary in summaries:
        d = summary["date"]
        eff_low = summary["tide_at_min"] + summary["setdown_at_min"]

        # Generate PNG
        png_bytes = depth_grid_to_png(
            summary["min_depth_grid"],
            no_go=MIN_NAVIGABLE_DEPTH_FT,
            caution=CAUTION_DEPTH_FT,
        )

        # Upload image
        image_url = upload_image(client, d, png_bytes)

        # Upsert metadata
        upsert_forecast(
            client,
            forecast_date=d,
            image_url=image_url,
            bounds=bounds_dict,
            effective_low_ft=eff_low,
            tide_ft=summary["tide_at_min"],
            setdown_ft=summary["setdown_at_min"],
            worst_hour=summary["min_depth_hour"],
        )

        print(f"  {d.strftime('%a %m/%d')}: eff low {eff_low:+.1f} ft → uploaded")

    # ------------------------------------------------------------------
    # Step 6: Cleanup old data
    # ------------------------------------------------------------------
    print("\n[6/6] Cleaning up...")
    delete_old_forecasts(client, keep_days=14)

    # ------------------------------------------------------------------
    # Optional: also generate local HTML
    # ------------------------------------------------------------------
    if args.local_html:
        print("\n  Generating local HTML map...")
        grid_bounds = bathy.get_grid_bounds_latlon()
        builder = MapBuilder(MAP_CENTER, MAP_ZOOM)
        m = builder.build_multi_day_map(summaries, grid_bounds)
        Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
        out_path = Path(OUTPUT_DIR) / f"depth_forecast_{now.strftime('%Y%m%d')}.html"
        builder.save(m, str(out_path))
        print(f"  Saved: {out_path}")

    # Summary
    print(f"\n{'=' * 62}")
    print(f"  {'Date':<12} {'Eff Low':>10}  {'Image'}")
    print(f"  {'-' * 55}")
    for s in summaries:
        eff = s["tide_at_min"] + s["setdown_at_min"]
        print(f"  {s['date'].strftime('%a %m/%d'):<12}{eff:>+8.1f} ft  ✓ uploaded")
    print(f"{'=' * 62}\n")


if __name__ == "__main__":
    main()
