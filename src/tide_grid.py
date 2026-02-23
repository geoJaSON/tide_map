"""
Spatial tide interpolation using Inverse Distance Weighting (IDW).

Given tide predictions at multiple NOAA stations, builds a per-pixel
tide height grid for the bathymetry coverage area.  Computation is done
on a coarse grid and bilinearly upsampled to full resolution for
memory efficiency.
"""

import numpy as np
from PIL import Image
from typing import Dict


class TideGridBuilder:
    """Builds per-pixel tide height grids via IDW from multiple stations."""

    def __init__(
        self,
        stations: Dict,
        grid_shape: tuple,
        grid_bounds: dict,
        power: int = 2,
        coarse_res: int = 300,
    ):
        """
        Args:
            stations:    {station_id: {"lat": float, "lon": float, ...}}
                         Only include stations that returned predictions.
            grid_shape:  (rows, cols) of the full-resolution bathy grid.
            grid_bounds: {"south", "north", "west", "east"} in decimal degrees.
            power:       IDW exponent (2 = standard inverse-square weighting).
            coarse_res:  Max dimension of the coarse computation grid.
        """
        self.stations = stations
        self.grid_shape = grid_shape
        self.grid_bounds = grid_bounds
        self.power = power
        self.coarse_res = coarse_res

        self._station_ids = list(stations.keys())
        self._coarse_shape, self._weights = self._compute_coarse_weights()

        print(f"  IDW grid: {self._coarse_shape[1]}x{self._coarse_shape[0]} coarse "
              f"→ {grid_shape[1]}x{grid_shape[0]} full  ({len(self._station_ids)} stations)")

    def _compute_coarse_weights(self):
        """Compute normalised IDW weights at coarse resolution.

        Returns (coarse_shape, weights) where weights is (N, c_rows, c_cols).
        """
        rows, cols = self.grid_shape
        aspect = cols / max(rows, 1)

        if cols >= rows:
            c_cols = min(self.coarse_res, cols)
            c_rows = max(1, int(c_cols / aspect))
        else:
            c_rows = min(self.coarse_res, rows)
            c_cols = max(1, int(c_rows * aspect))

        coarse_shape = (c_rows, c_cols)

        # Lat/lon meshgrid at coarse resolution
        lats = np.linspace(
            self.grid_bounds["north"], self.grid_bounds["south"], c_rows
        )
        lons = np.linspace(
            self.grid_bounds["west"], self.grid_bounds["east"], c_cols
        )
        lon_grid, lat_grid = np.meshgrid(lons, lats)

        N = len(self._station_ids)
        weights = np.zeros((N, c_rows, c_cols), dtype=np.float32)

        for i, sid in enumerate(self._station_ids):
            s = self.stations[sid]
            # Equirectangular distance approximation (degrees → km)
            dlat = lat_grid - s["lat"]
            dlon = (lon_grid - s["lon"]) * np.cos(np.radians(s["lat"]))
            dist_km = np.sqrt(dlat ** 2 + dlon ** 2) * 111.0
            dist_km = np.maximum(dist_km, 0.1)  # 100 m floor to avoid singularity
            weights[i] = 1.0 / (dist_km ** self.power)

        # Normalise so weights sum to 1 at each pixel
        total = weights.sum(axis=0, keepdims=True)
        total = np.maximum(total, 1e-12)
        weights /= total

        return coarse_shape, weights

    def build_tide_grid(self, station_tides: Dict[str, float]) -> np.ndarray:
        """
        Build a full-resolution tide height grid for one time step.

        Args:
            station_tides: {station_id: height_ft} for the current hour.
                           May be a subset of the stations the builder
                           was initialised with.

        Returns:
            np.ndarray of shape grid_shape with spatially interpolated
            tide heights in feet.
        """
        active_ids = [sid for sid in self._station_ids if sid in station_tides]

        if not active_ids:
            raise ValueError("No station tide data available for this hour")

        # Single station → uniform grid (skip IDW)
        if len(active_ids) == 1:
            val = station_tides[active_ids[0]]
            return np.full(self.grid_shape, val, dtype=np.float32)

        # Subset and re-normalise weights for active stations only
        indices = [self._station_ids.index(sid) for sid in active_ids]
        active_w = self._weights[indices].copy()
        total = active_w.sum(axis=0, keepdims=True)
        total = np.maximum(total, 1e-12)
        active_w /= total

        # Weighted sum at coarse resolution
        coarse_tide = np.zeros(self._coarse_shape, dtype=np.float32)
        for i, sid in enumerate(active_ids):
            coarse_tide += active_w[i] * station_tides[sid]

        # Upscale to full resolution via bilinear interpolation
        img = Image.fromarray(coarse_tide, mode="F")
        full_tide = np.array(
            img.resize(
                (self.grid_shape[1], self.grid_shape[0]),
                Image.BILINEAR,
            )
        )

        return full_tide
