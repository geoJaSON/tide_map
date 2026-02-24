"""
Combines bathymetry, tide predictions, and wind setdown into
predicted depth grids.

Supports spatially varying tides via TideGridBuilder (IDW from
multiple NOAA stations) and spatially varying setdown via the
Sibul equation with fetch grids.
"""

import warnings
import numpy as np
from datetime import datetime
from typing import Dict, List, Optional

from src.tide_grid import TideGridBuilder


class DepthCalculator:
    """
    At each grid cell, for each forecast hour:

        predicted_depth = static_depth_mllw + tide_height + wind_setdown

    tide_height is spatially interpolated across the grid when a
    TideGridBuilder is provided (multi-station mode).  Otherwise it
    falls back to a uniform scalar (single-station mode).

    wind_setdown is spatially varying when a setdown_model is provided,
    computed on-demand from wind data for each snapshot hour.
    """

    def __init__(
        self,
        static_depth_grid: np.ndarray,
        tide_grid_builder: Optional[TideGridBuilder] = None,
        setdown_model=None,
    ):
        """
        Args:
            static_depth_grid:  2D array of depth below MLLW (feet).
                                Positive = water depth at mean lower low water.
                                NaN = no bathymetry data.
            tide_grid_builder:  If provided, enables spatial tide interpolation.
            setdown_model:      If provided (SibulSetdownModel), enables spatial
                                wind setdown.  Must have compute_setdown_grid().
        """
        self.static_depth = static_depth_grid
        self.tide_grid_builder = tide_grid_builder
        self.setdown_model = setdown_model

    def compute_depth_for_hour(
        self, tide_height, setdown
    ) -> np.ndarray:
        """Compute predicted depth grid for a single hour.

        tide_height and setdown can each be a scalar float or a 2D
        ndarray matching the static_depth grid shape (numpy broadcasting
        handles both).
        """
        water_level = tide_height + setdown
        return self.static_depth + water_level

    def compute_snapshots(
        self,
        multi_predictions: Dict[str, List[Dict]],
        wind_forecasts: List[Dict],
        target_dates: List,
        snapshot_hours: List[int] = (7, 12, 17),
        save_intermediates: bool = False,
    ) -> List[Dict]:
        """
        Compute a depth grid at each snapshot hour for each target date.

        Args:
            multi_predictions: {station_id: [{"time": dt, "height_ft": float}]}
            wind_forecasts:    [{"time": dt, "wind_speed_mph": float,
                                 "wind_direction_deg": float}]
            target_dates:      list of date or datetime objects
            snapshot_hours:    list of hours (24h) to snapshot, e.g. [7, 12, 17]
            save_intermediates: if True, include "tide_grid" and "setdown_grid"
                                arrays in each snapshot dict (for debug rasters).

        Returns list of:
        {
            "date":        date object,
            "hour":        int (7, 12, or 17),
            "time":        datetime of the snapshot,
            "depth_grid":  np.ndarray (predicted depth at that hour),
            "tide_ft":     float (mean tide level across stations),
            "setdown_ft":  float (mean setdown across grid),
            "tide_grid":   np.ndarray (only if save_intermediates),
            "setdown_grid": np.ndarray (only if save_intermediates),
        }

        Notes:
        - If a snapshot hour has no tide data, it is skipped.
        - Wind uses nearest available hour if exact match missing.
        - Setdown grid is computed on-demand (one at a time, not cached).
        """
        # Build wind lookup by hour
        wind_by_hour = {}
        wind_times = []
        for wf in wind_forecasts:
            key = wf["time"].replace(minute=0, second=0, microsecond=0)
            wind_by_hour[key] = wf
            wind_times.append(key)
        wind_times = sorted(set(wind_times))

        def _get_wind_for_hour(hour: datetime) -> Dict:
            key = hour.replace(minute=0, second=0, microsecond=0)
            if key in wind_by_hour:
                return wind_by_hour[key]
            if not wind_times:
                return {"wind_speed_mph": 0.0, "wind_direction_deg": 0.0}
            nearest = min(wind_times, key=lambda t: abs((t - key).total_seconds()))
            return wind_by_hour[nearest]

        # Build tide lookup: {hour: {station_id: height_ft}}
        tide_by_hour: Dict[datetime, Dict[str, float]] = {}
        for sid, preds in multi_predictions.items():
            for p in preds:
                hour = p["time"].replace(minute=0, second=0, microsecond=0)
                if hour not in tide_by_hour:
                    tide_by_hour[hour] = {}
                tide_by_hour[hour][sid] = p["height_ft"]

        snapshots = []
        for target_date in target_dates:
            if hasattr(target_date, "date"):
                d = target_date.date()
            else:
                d = target_date

            for sh in snapshot_hours:
                # Find the matching hour in tide data
                target_hour = None
                for h in tide_by_hour:
                    if h.date() == d and h.hour == sh:
                        target_hour = h
                        break
                if target_hour is None:
                    continue

                station_tides = tide_by_hour[target_hour]
                if not station_tides:
                    continue

                # Compute spatial setdown grid (or zero if no model)
                wind = _get_wind_for_hour(target_hour)
                if self.setdown_model:
                    setdown_grid = self.setdown_model.compute_setdown_grid(
                        wind["wind_speed_mph"], wind["wind_direction_deg"]
                    )
                    # Show worst-case (most negative) setdown for display/upload
                    setdown_display = float(np.nanmin(setdown_grid))
                else:
                    setdown_grid = 0.0
                    setdown_display = 0.0

                # Build spatial tide grid or use uniform scalar
                if self.tide_grid_builder and len(station_tides) > 0:
                    tide_val = self.tide_grid_builder.build_tide_grid(station_tides)
                    tide_mean = float(np.mean(list(station_tides.values())))
                else:
                    tide_val = float(np.mean(list(station_tides.values())))
                    tide_mean = tide_val

                depth_grid = self.compute_depth_for_hour(tide_val, setdown_grid)

                snap = {
                    "date": d,
                    "hour": sh,
                    "time": target_hour,
                    "depth_grid": depth_grid,
                    "tide_ft": tide_mean,
                    "setdown_ft": setdown_display,
                }
                if save_intermediates:
                    snap["tide_grid"] = tide_val if isinstance(tide_val, np.ndarray) else np.full_like(self.static_depth, tide_val)
                    snap["setdown_grid"] = setdown_grid if isinstance(setdown_grid, np.ndarray) else np.full_like(self.static_depth, setdown_grid)
                snapshots.append(snap)

        return snapshots
