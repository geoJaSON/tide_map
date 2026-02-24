"""
South Louisiana Navigable Depth Predictor

Combines bathymetry, tide predictions from multiple NOAA stations,
and wind forecasts to produce a color-coded map showing where boats
can safely navigate.  Generates snapshots at 7 AM, 12 PM, and 5 PM.

Usage:
    python main.py                         # 7-day forecast
    python main.py --days 3                # 3-day forecast
    python main.py --output my_report.html # custom filename

Requires:
    - BlueTopo .tiff files in data/bluetopo/ (run build_vrt.py first)
    - Fetch raster (4-band GeoTIFF) in data/fetch/
    - Internet access for NOAA and NWS APIs
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_bounds as transform_from_bounds

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
from src.map_builder import MapBuilder


def save_debug_raster(data: np.ndarray, path: str, bounds: dict):
    """Write a 2D float32 array as a single-band GeoTIFF (EPSG:4326)."""
    rows, cols = data.shape
    transform = transform_from_bounds(
        bounds["west"], bounds["south"],
        bounds["east"], bounds["north"],
        cols, rows,
    )
    with rasterio.open(
        path, "w", driver="GTiff",
        height=rows, width=cols, count=1,
        dtype="float32", crs="EPSG:4326",
        transform=transform, compress="deflate",
    ) as dst:
        dst.write(data.astype(np.float32), 1)


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
    parser.add_argument(
        "--debug-rasters", action="store_true",
        help="Save tide, setdown, static depth, and vdatum grids as GeoTIFFs for calibration",
    )
    args = parser.parse_args()

    print("=" * 62)
    print("  South Louisiana Depth Forecast")
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
    # Step 2b: Apply VDatum offsets
    # ------------------------------------------------------------------
    print("\n[2b/6] Building spatial VDatum offset grid...")
    vdatum_offsets = {sid: info["vdatum_offset"] for sid, info in active_stations.items()}
    vdatum_grid = tide_builder.build_tide_grid(vdatum_offsets)
    static_depth = bathy.get_static_depth_mllw_ft(vdatum_grid)

    total_cells = static_depth.size
    nan_cells = np.isnan(static_depth).sum()
    nan_pct = nan_cells / total_cells * 100 if total_cells > 0 else 0
    print(f"  Data coverage: {100 - nan_pct:.0f}%  ({nan_pct:.0f}% no-data)")

    # ------------------------------------------------------------------
    # Step 3: Load fetch raster
    # ------------------------------------------------------------------
    print("\n[3/6] Loading fetch raster...")
    fetch_path = Path(FETCH_RASTER_PATH)
    if not fetch_path.exists():
        print(f"  ERROR: Fetch raster not found: {FETCH_RASTER_PATH}")
        print(f"  Create a 4-band GeoTIFF (N/E/S/W fetch in feet).")
        sys.exit(1)

    fetch_grids = load_fetch_grids(str(fetch_path), BOUNDS, bathy.shape)
    setdown_model = SibulSetdownModel(fetch_grids, avg_depth_ft=SIBUL_AVG_DEPTH_FT)
    print(f"  Sibul avg depth: {SIBUL_AVG_DEPTH_FT} ft")

    # ------------------------------------------------------------------
    # Step 4: Fetch wind forecast
    # ------------------------------------------------------------------
    print("\n[4/6] Fetching wind forecast...")
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
    # Step 5: Compute snapshot depth grids and generate map
    # ------------------------------------------------------------------
    hours_label = ", ".join(f"{h}:00" for h in SNAPSHOT_HOURS)
    print(f"\n[5/6] Computing depth snapshots ({hours_label})...")
    calculator = DepthCalculator(
        static_depth,
        tide_grid_builder=tide_builder,
        setdown_model=setdown_model,
    )
    target_dates = [now + timedelta(days=d) for d in range(args.days)]
    snapshots = calculator.compute_snapshots(
        multi_preds, wind_forecast, target_dates,
        snapshot_hours=SNAPSHOT_HOURS,
        save_intermediates=args.debug_rasters,
    )

    if not snapshots:
        print("\n  ERROR: No snapshots could be computed.")
        print("  Check that tide predictions and wind forecasts have overlapping dates.")
        sys.exit(1)

    # ------------------------------------------------------------------
    # Step 6: Build map
    # ------------------------------------------------------------------
    print(f"\n[6/6] Generating map...")
    grid_bounds = bathy.get_grid_bounds_latlon()
    builder = MapBuilder(MAP_CENTER, MAP_ZOOM)
    m = builder.build_snapshot_map(snapshots, grid_bounds)

    # Save output
    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
    if args.output:
        out_path = Path(OUTPUT_DIR) / args.output
    else:
        out_path = Path(OUTPUT_DIR) / f"depth_forecast_{now.strftime('%Y%m%d')}.html"

    builder.save(m, str(out_path))

    # ------------------------------------------------------------------
    # Debug rasters (optional)
    # ------------------------------------------------------------------
    if args.debug_rasters:
        debug_dir = Path(OUTPUT_DIR) / "debug_rasters"
        debug_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n  Saving debug rasters to {debug_dir}/...")

        save_debug_raster(static_depth, str(debug_dir / "static_depth_mllw.tif"), BOUNDS)
        save_debug_raster(vdatum_grid, str(debug_dir / "vdatum_offset.tif"), BOUNDS)

        for s in snapshots:
            tag = f"{s['date'].strftime('%Y%m%d')}_{s['hour']:02d}"
            if "tide_grid" in s:
                save_debug_raster(s["tide_grid"], str(debug_dir / f"tide_{tag}.tif"), BOUNDS)
            if "setdown_grid" in s:
                save_debug_raster(s["setdown_grid"], str(debug_dir / f"setdown_{tag}.tif"), BOUNDS)
            save_debug_raster(s["depth_grid"], str(debug_dir / f"depth_{tag}.tif"), BOUNDS)

        n_files = 2 + len(snapshots) * 3  # static + vdatum + (tide + setdown + depth) per snap
        print(f"  Wrote {n_files} GeoTIFFs (EPSG:4326, float32)")

    # ------------------------------------------------------------------
    # Print summary table
    # ------------------------------------------------------------------
    n_dates = len(set(s["date"] for s in snapshots))
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
    print(f"{'=' * 62}")
    print(f"\n  {len(snapshots)} snapshots across {n_dates} day(s)")
    print(f"  Map saved to: {out_path}")
    print(f"  Open in any browser to view.\n")


if __name__ == "__main__":
    main()
