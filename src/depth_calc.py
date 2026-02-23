"""
Combines bathymetry, tide predictions, and wind setdown into
daily predicted depth grids.

Supports spatially varying tides via TideGridBuilder (IDW from
multiple NOAA stations).
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

    Each daily summary uses the per-pixel MINIMUM across the day
    (worst case for navigation safety).
    """

    def __init__(
        self,
        static_depth_grid: np.ndarray,
        tide_grid_builder: Optional[TideGridBuilder] = None,
    ):
        """
        Args:
            static_depth_grid:  2D array of depth below MLLW (feet).
                                Positive = water depth at mean lower low water.
                                NaN = no bathymetry data.
            tide_grid_builder:  If provided, enables spatial tide interpolation.
        """
        self.static_depth = static_depth_grid
        self.tide_grid_builder = tide_grid_builder

    def compute_depth_for_hour(
        self, tide_height, setdown_ft: float
    ) -> np.ndarray:
        """Compute predicted depth grid for a single hour.

        tide_height can be a scalar float or a 2D ndarray matching the
        static_depth grid shape (numpy broadcasting handles both).
        """
        water_level = tide_height + setdown_ft
        return self.static_depth + water_level

    def compute_daily_summaries(
        self,
        multi_predictions: Dict[str, List[Dict]],
        setdown_series: List[Dict],
        target_dates: List,
    ) -> List[Dict]:
        """
        For each target date, compute worst-case (minimum) depth grid.

        Args:
            multi_predictions: {station_id: [{"time": dt, "height_ft": float}]}
                               Dict keyed by station ID.  For single-station
                               mode, pass a dict with one key.
            setdown_series:    [{"time": dt, "setdown_ft": float}]
            target_dates:      list of date or datetime objects

        Returns list of:
        {
            "date":            date object,
            "min_depth_grid":  np.ndarray (worst case per pixel),
            "min_depth_hour":  datetime of worst overall conditions,
            "avg_depth_grid":  np.ndarray (average across the day),
            "tide_at_min":     float (mean tide level at worst hour),
            "setdown_at_min":  float (setdown at worst hour),
        }

        Aggregation notes:
        - Hour alignment: Only hours present in tide predictions are used.
          Setdown for a missing wind hour uses the nearest available wind hour
          (avoids assuming calm when wind simply wasn't forecast for that hour).
        - Worst hour: Defined as the hour with the lowest spatial *mean* depth,
          not the hour when the single shallowest pixel is lowest.
        - Calendar day: Uses the date of each hour in local (naive) time.
        """
        # Build lookup of setdown by hour
        setdown_by_hour = {}
        setdown_times = []
        for sd in setdown_series:
            key = sd["time"].replace(minute=0, second=0, microsecond=0)
            setdown_by_hour[key] = sd["setdown_ft"]
            setdown_times.append(key)
        setdown_times = sorted(set(setdown_times))

        def _get_setdown_for_hour(hour: datetime) -> float:
            """Use setdown at hour if available; else nearest available (avoid assuming calm)."""
            key = hour.replace(minute=0, second=0, microsecond=0)
            if key in setdown_by_hour:
                return setdown_by_hour[key]
            if not setdown_times:
                return 0.0
            # Nearest time (by absolute difference)
            nearest = min(setdown_times, key=lambda t: abs((t - key).total_seconds()))
            return setdown_by_hour[nearest]

        # Build lookup: {hour: {station_id: height_ft}}
        tide_by_hour: Dict[datetime, Dict[str, float]] = {}
        all_hours = set()
        for sid, preds in multi_predictions.items():
            for p in preds:
                hour = p["time"].replace(minute=0, second=0, microsecond=0)
                all_hours.add(hour)
                if hour not in tide_by_hour:
                    tide_by_hour[hour] = {}
                tide_by_hour[hour][sid] = p["height_ft"]

        summaries = []
        for target_date in target_dates:
            if hasattr(target_date, "date"):
                d = target_date.date()
            else:
                d = target_date

            # Filter hours for this calendar day
            day_hours = sorted([h for h in all_hours if h.date() == d])
            if not day_hours:
                continue

            # Incremental min/sum — avoids stacking all hourly grids
            # which would blow memory for large grids (24 h × grid_size).
            min_depth_grid = None
            sum_depth_grid = None
            n_hours = 0
            worst_mean = float("inf")
            worst_time = None
            worst_tide_mean = 0.0
            worst_setdown = 0.0

            for hour in day_hours:
                station_tides = tide_by_hour.get(hour, {})
                if not station_tides:
                    continue

                sd_ft = _get_setdown_for_hour(hour)

                # Build spatial tide grid or use uniform scalar
                if self.tide_grid_builder and len(station_tides) > 0:
                    tide_val = self.tide_grid_builder.build_tide_grid(station_tides)
                    tide_mean = float(np.mean(list(station_tides.values())))
                else:
                    tide_val = float(np.mean(list(station_tides.values())))
                    tide_mean = tide_val

                depth_grid = self.compute_depth_for_hour(tide_val, sd_ft)

                # Update running min
                if min_depth_grid is None:
                    min_depth_grid = depth_grid.copy()
                    sum_depth_grid = np.where(
                        np.isnan(depth_grid), 0.0, depth_grid
                    ).astype(np.float64)
                else:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", RuntimeWarning)
                        min_depth_grid = np.fmin(min_depth_grid, depth_grid)
                    sum_depth_grid += np.where(
                        np.isnan(depth_grid), 0.0, depth_grid
                    )
                n_hours += 1

                # Track worst hour (lowest mean depth)
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    hmean = float(np.nanmean(depth_grid))
                if hmean < worst_mean:
                    worst_mean = hmean
                    worst_time = hour
                    worst_tide_mean = tide_mean
                    worst_setdown = sd_ft

            if min_depth_grid is None or n_hours == 0:
                continue

            avg_depth_grid = (sum_depth_grid / n_hours).astype(np.float32)
            # Restore NaN where static depth is NaN
            nan_mask = np.isnan(self.static_depth)
            avg_depth_grid[nan_mask] = np.nan

            summaries.append({
                "date": d,
                "min_depth_grid": min_depth_grid,
                "min_depth_hour": worst_time,
                "avg_depth_grid": avg_depth_grid,
                "tide_at_min": worst_tide_mean,
                "setdown_at_min": worst_setdown,
            })

        return summaries
