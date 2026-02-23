"""
South Louisiana Navigable Depth Predictor

Combines bathymetry, tide predictions from multiple NOAA stations,
and wind forecasts to produce a color-coded map showing where boats
can safely navigate.

Usage:
    python main.py                         # 7-day forecast
    python main.py --days 3                # 3-day forecast
    python main.py --output my_report.html # custom filename

Requires:
    - BlueTopo .tiff files in data/bluetopo/ (run build_vrt.py first)
    - Internet access for NOAA and NWS APIs
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
    IDW_POWER,
    IDW_COARSE_RES,
    MAX_GRID_DIM,
    WORK_HOURS,
)
from src.bathymetry import BathymetryGrid
from src.tides import TideClient
from src.wind import WindForecastClient
from src.setdown import WindSetdownModel
from src.tide_grid import TideGridBuilder
from src.depth_calc import DepthCalculator
from src.map_builder import MapBuilder


def main():
    parser = argparse.ArgumentParser(
        description="Predict navigable water depth for south Louisiana"
    )
    parser.add_argument(
        "--days", type=int, default=7,
        help="Number of forecast days (default: 7)",
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Output HTML filename (default: auto-generated with date)",
    )
    args = parser.parse_args()

    print("=" * 62)
    print("  South Louisiana Depth Forecast")
    print("=" * 62)

    # ------------------------------------------------------------------
    # Step 1: Load bathymetry
    # ------------------------------------------------------------------
    print("\n[1/5] Loading bathymetry...")
    try:
        bathy = BathymetryGrid(BLUETOPO_DIR, BOUNDS, max_dim=MAX_GRID_DIM)
    except FileNotFoundError as e:
        print(f"\n  ERROR: {e}")
        sys.exit(1)

    static_depth = bathy.get_static_depth_mllw_ft(NAVD88_TO_MLLW_OFFSET_FT)
    total_cells = static_depth.size
    nan_cells = np.isnan(static_depth).sum()
    nan_pct = nan_cells / total_cells * 100 if total_cells > 0 else 0

    print(f"  Grid: {bathy.shape[1]} x {bathy.shape[0]} pixels")
    print(f"  Data coverage: {100 - nan_pct:.0f}%  ({nan_pct:.0f}% no-data)")

    # ------------------------------------------------------------------
    # Step 2: Fetch tide predictions from all stations
    # ------------------------------------------------------------------
    print("\n[2/5] Fetching tide predictions...")
    tides = TideClient(CACHE_DIR)
    now = datetime.now()

    multi_preds = tides.get_multi_station_predictions(
        list(TIDE_STATIONS.keys()), now, num_days=args.days
    )

    if not multi_preds:
        print("\n  ERROR: Could not fetch tide predictions from any station.")
        sys.exit(1)

    active_stations = {sid: TIDE_STATIONS[sid] for sid in multi_preds}
    print(f"  Active stations: {len(active_stations)} of {len(TIDE_STATIONS)}")
    for sid, info in active_stations.items():
        n_pts = len(multi_preds[sid])
        print(f"    {info['name']} ({sid}): {n_pts} predictions")

    # Build spatial tide interpolator
    tide_builder = TideGridBuilder(
        active_stations, bathy.shape, BOUNDS, IDW_POWER, IDW_COARSE_RES
    )

    # ------------------------------------------------------------------
    # Step 3: Fetch wind forecast
    # ------------------------------------------------------------------
    print("\n[3/5] Fetching wind forecast...")
    wind_client = WindForecastClient()
    wind_forecast = wind_client.get_hourly_wind_forecast(
        WIND_FORECAST_LAT, WIND_FORECAST_LON
    )

    if not wind_forecast:
        print("  WARNING: No wind forecast available. Proceeding with tides only.")
        first_preds = next(iter(multi_preds.values()))
        wind_forecast = [
            {
                "time": t["time"],
                "wind_speed_mph": 0.0,
                "wind_direction": "N",
                "wind_direction_deg": 0.0,
            }
            for t in first_preds
        ]
    else:
        print(f"  Got {len(wind_forecast)} hourly forecasts")

    # ------------------------------------------------------------------
    # Step 4: Calculate wind setdown
    # ------------------------------------------------------------------
    print("\n[4/5] Computing wind setdown...")
    setdown_model = WindSetdownModel(coeff=SETDOWN_COEFF)
    setdown_series = setdown_model.calculate_setdown_timeseries(wind_forecast)

    if setdown_series:
        worst = min(setdown_series, key=lambda x: x["setdown_ft"])
        best = max(setdown_series, key=lambda x: x["setdown_ft"])
        print(f"  Max setdown:  {worst['setdown_ft']:+.1f} ft at {worst['time'].strftime('%a %I %p')}")
        print(f"  Max set-up:   {best['setdown_ft']:+.1f} ft at {best['time'].strftime('%a %I %p')}")

    # ------------------------------------------------------------------
    # Step 5: Compute depth grids and generate map
    # ------------------------------------------------------------------
    print(f"\n[5/5] Generating forecast map (work hours {WORK_HOURS[0]:02d}:00–{WORK_HOURS[1]:02d}:00)...")
    calculator = DepthCalculator(static_depth, tide_grid_builder=tide_builder)
    target_dates = [now + timedelta(days=d) for d in range(args.days)]
    summaries = calculator.compute_daily_summaries(
        multi_preds, setdown_series, target_dates, work_hours=WORK_HOURS
    )

    if not summaries:
        print("\n  ERROR: No daily summaries could be computed.")
        print("  Check that tide predictions and wind forecasts have overlapping dates.")
        sys.exit(1)

    # Build the map
    grid_bounds = bathy.get_grid_bounds_latlon()
    builder = MapBuilder(MAP_CENTER, MAP_ZOOM)
    m = builder.build_multi_day_map(summaries, grid_bounds)

    # Save output
    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
    if args.output:
        out_path = Path(OUTPUT_DIR) / args.output
    else:
        out_path = Path(OUTPUT_DIR) / f"depth_forecast_{now.strftime('%Y%m%d')}.html"

    builder.save(m, str(out_path))

    # ------------------------------------------------------------------
    # Print summary table
    # ------------------------------------------------------------------
    print(f"\n{'=' * 62}")
    print(f"  {'Date':<12} {'Tide Low':>10} {'Setdown':>10} {'Eff Low':>10}")
    print(f"  {'-' * 50}")
    for s in summaries:
        eff_low = s["tide_at_min"] + s["setdown_at_min"]
        print(
            f"  {s['date'].strftime('%a %m/%d'):<12}"
            f"{s['tide_at_min']:>+8.1f} ft"
            f"{s['setdown_at_min']:>+8.1f} ft"
            f"{eff_low:>+8.1f} ft"
        )
    print(f"{'=' * 62}")
    print(f"\n  Map saved to: {out_path}")
    print(f"  Open in any browser to view.\n")


if __name__ == "__main__":
    main()
