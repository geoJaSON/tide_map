"""
Wind setup/setdown model with spatial fetch.

Uses the USACE wind stress formula to compute wind-driven water level
changes that vary across the grid based on fetch distance and wind
direction:

    S = Cd × U² × F / (g × d)

Where Cd is the surface drag coefficient (~2.5e-6), U is wind speed
(ft/s), F is fetch (ft), g is gravity (ft/s²), and d is average
basin depth (ft).

A linear tilt model distributes the setup/setdown spatially:
  - Upwind shore: water drops (setdown)
  - Downwind shore: water rises (setup)
"""

import numpy as np
import rasterio
from rasterio.windows import from_bounds
from typing import Dict, Tuple


# Gravitational constant (ft/s²)
G_FPS2 = 32.174

# mph to ft/s conversion
MPH_TO_FPS = 5280.0 / 3600.0


def load_fetch_grids(
    raster_path: str,
    bounds: dict,
    target_shape: Tuple[int, int],
) -> Dict[str, np.ndarray]:
    """
    Load an 8-band fetch raster and resample to match the depth grid.

    Band order: 1=N, 2=S, 3=E, 4=W, 5=NE, 6=NW, 7=SE, 8=SW.
    Values in feet.

    Args:
        raster_path:  Path to 8-band GeoTIFF.
        bounds:       Dict with north/south/east/west in decimal degrees.
        target_shape: (rows, cols) to resample to (must match depth grid).

    Returns:
        Dict with 8 direction keys, each a 2D float32 array (feet).
    """
    src = rasterio.open(raster_path)

    window = from_bounds(
        bounds["west"], bounds["south"],
        bounds["east"], bounds["north"],
        transform=src.transform,
    )
    window = window.intersection(
        rasterio.windows.Window(0, 0, src.width, src.height)
    )

    out_h, out_w = target_shape

    # Band order in the GeoTIFF
    band_labels = ['N', 'S', 'E', 'W', 'NE', 'NW', 'SE', 'SW']
    grids = {}

    for band_idx, label in enumerate(band_labels, start=1):
        if band_idx > src.count:
            break
        data = src.read(
            band_idx,
            window=window,
            out_shape=(out_h, out_w),
            resampling=rasterio.enums.Resampling.bilinear,
        ).astype(np.float32)
        # Clamp negative values to zero (no negative fetch)
        np.maximum(data, 0.0, out=data)
        grids[label] = data

    src.close()
    n_bands = len(grids)
    print(f"  Fetch raster: {raster_path}")
    print(f"  Resampled to {out_w}x{out_h} ({n_bands} bands: {', '.join(grids.keys())})")
    return grids


class WindSetupModel:
    """
    Spatially varying wind setup/setdown using USACE wind stress formula.

    Requires an 8-direction fetch grid (N, NE, E, SE, S, SW, W, NW) and
    an average basin depth.  For each wind forecast hour, produces a 2D
    grid of water level change (negative = setdown, positive = setup).
    """

    # Surface drag coefficient (dimensionless).
    # USACE typical range: 1.5e-6 to 3.3e-6.  2.5e-6 is a common default.
    DRAG_COEFF = 2.5e-6

    # 8 directions at 45° intervals, ordered by compass bearing
    _DIR_ORDER = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']

    def __init__(
        self,
        fetch_grids: Dict[str, np.ndarray],
        avg_depth_ft: float = 5.0,
    ):
        """
        Args:
            fetch_grids:   Dict with direction keys (N, NE, E, SE, S, SW, W, NW),
                           each a 2D array (ft).
            avg_depth_ft:  Representative average basin depth (ft).
        """
        # Store grids ordered by bearing: N(0°), NE(45°), E(90°), ..., NW(315°)
        self._fetch_grids = [fetch_grids[d] for d in self._DIR_ORDER]
        self._n_dirs = len(self._fetch_grids)  # 8
        self._sector_deg = 360.0 / self._n_dirs  # 45°
        self.d = avg_depth_ft
        # Keep a reference grid for shape
        self._ref_grid = self._fetch_grids[0]

    def _interpolate_fetch(self, direction_deg: float) -> np.ndarray:
        """
        Get fetch grid for an arbitrary direction by interpolating
        between the two nearest fetch grids.

        Args:
            direction_deg: Compass direction (0=N, 90=E, 180=S, 270=W).

        Returns:
            2D array of fetch distance (ft) for the given direction.
        """
        d = direction_deg % 360
        sector = d / self._sector_deg  # 0–8
        lower_idx = int(sector) % self._n_dirs
        upper_idx = (lower_idx + 1) % self._n_dirs
        weight = sector - int(sector)

        return self._fetch_grids[lower_idx] * (1.0 - weight) + self._fetch_grids[upper_idx] * weight

    def _wind_setup(self, U_fps: float, F: np.ndarray) -> np.ndarray:
        """
        USACE wind stress formula for wind setup.

            S = Cd × U² × F / (g × d)

        Args:
            U_fps:  Wind speed in ft/s (scalar).
            F:      Fetch distance in ft (2D array).

        Returns:
            2D array of setup S in feet (always positive).
        """
        d = self.d
        if U_fps <= 0 or d <= 0:
            return np.zeros_like(F)

        S = self.DRAG_COEFF * (U_fps ** 2) * F / (G_FPS2 * d)
        return S

    def compute_setdown_grid(
        self,
        wind_speed_mph: float,
        wind_direction_deg: float,
    ) -> np.ndarray:
        """
        Compute spatial water level change for a given wind condition.

        Uses the USACE wind stress formula with a linear tilt model:
        - Upwind shore (small upwind fetch): negative (setdown)
        - Downwind shore (large upwind fetch): positive (setup)

        Args:
            wind_speed_mph:      Wind speed in mph.
            wind_direction_deg:  Direction wind is coming FROM (meteorological).

        Returns:
            2D array of water level change in feet.
            Negative = water drops (dangerous).
            Positive = water rises.
        """
        if wind_speed_mph <= 0:
            return np.zeros_like(self._ref_grid)

        U_fps = wind_speed_mph * MPH_TO_FPS

        # Upwind fetch: looking toward where wind comes FROM
        upwind_fetch = self._interpolate_fetch(wind_direction_deg)
        # Downwind fetch: looking in the direction wind blows TO
        downwind_dir = (wind_direction_deg + 180.0) % 360.0
        downwind_fetch = self._interpolate_fetch(downwind_dir)

        total_fetch = upwind_fetch + downwind_fetch

        # Position fraction: 0 at upwind shore, 1 at downwind shore
        with np.errstate(invalid='ignore', divide='ignore'):
            fraction = np.where(total_fetch > 0, upwind_fetch / total_fetch, 0.5)

        # Wind setup magnitude using total basin fetch
        S_total = self._wind_setup(U_fps, total_fetch)

        # Linear tilt: -S at upwind, +S at downwind
        water_level_change = S_total * (2.0 * fraction - 1.0)

        return water_level_change


# Backward-compatible alias
SibulSetdownModel = WindSetupModel
