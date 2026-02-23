"""
Combines bathymetry, tide predictions, and wind setdown into
predicted depth grids.

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

    def compute_snapshots(
        self,
        multi_predictions: Dict[str, List[Dict]],
        setdown_series: List[Dict],
        target_dates: List,
        snapshot_hours: List[int] = (7, 12, 17),
    ) -> List[Dict]:
        """
        Compute a depth grid at each snapshot hour for each target date.

        Args:
            multi_predictions: {station_id: [{"time": dt, "height_ft": float}]}
            setdown_series:    [{"time": dt, "setdown_ft": float}]
            target_dates:      list of date or datetime objects
            snapshot_hours:    list of hours (24h) to snapshot, e.g. [7, 12, 17]

        Returns list of:
        {
            "date":        date object,
            "hour":        int (7, 12, or 17),
            "time":        datetime of the snapshot,
            "depth_grid":  np.ndarray (predicted depth at that hour),
            "tide_ft":     float (mean tide level across stations),
            "setdown_ft":  float (wind setdown at that hour),
        }

        Notes:
        - If a snapshot hour has no tide data, it is skipped.
        - Setdown uses nearest available wind hour if exact match missing.
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
            key = hour.replace(minute=0, second=0, microsecond=0)
            if key in setdown_by_hour:
                return setdown_by_hour[key]
            if not setdown_times:
                return 0.0
            nearest = min(setdown_times, key=lambda t: abs((t - key).total_seconds()))
            return setdown_by_hour[nearest]

        # Build lookup: {hour: {station_id: height_ft}}
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

                sd_ft = _get_setdown_for_hour(target_hour)

                # Build spatial tide grid or use uniform scalar
                if self.tide_grid_builder and len(station_tides) > 0:
                    tide_val = self.tide_grid_builder.build_tide_grid(station_tides)
                    tide_mean = float(np.mean(list(station_tides.values())))
                else:
                    tide_val = float(np.mean(list(station_tides.values())))
                    tide_mean = tide_val

                depth_grid = self.compute_depth_for_hour(tide_val, sd_ft)

                snapshots.append({
                    "date": d,
                    "hour": sh,
                    "time": target_hour,
                    "depth_grid": depth_grid,
                    "tide_ft": tide_mean,
                    "setdown_ft": sd_ft,
                })

        return snapshots
