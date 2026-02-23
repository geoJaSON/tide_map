"""
Wind setdown model for Terrebonne Bay.

A sustained north wind pushes surface water south through the passes into
the Gulf, lowering bay water levels.  This module estimates that effect.

Phase 1: Empirical coefficient model (current).
Phase 2: Regression trained on historical NOAA observed-vs-predicted residuals.
"""

import numpy as np
from typing import List, Dict


class WindSetdownModel:
    """
    Estimates wind-driven water level changes.

    The key parameter is `coeff` (ft per mph²).  It is calibrated so that
    a 20 mph due-north wind produces roughly 1.2 ft of setdown:
        0.003 * 20² * cos(0°) = 0.003 * 400 * 1.0 = 1.2 ft

    Increase coeff if field observations show more setdown than predicted.
    """

    def __init__(self, coeff: float = 0.003):
        self.coeff = coeff

    def calculate_setdown_ft(
        self,
        wind_speed_mph: float,
        wind_direction_deg: float,
    ) -> float:
        """
        Calculate water level change due to wind.

        Args:
            wind_speed_mph:      Wind speed in mph.
            wind_direction_deg:  Direction wind is coming FROM (meteorological).
                                 0 = north, 90 = east, 180 = south, 270 = west.

        Returns:
            Water level change in feet.
            Negative = water pushed out (lower level, dangerous).
            Positive = water pushed in  (higher level).
        """
        # North-component of the wind.
        # Wind FROM north (0°) → cos(0) = 1.0 → maximum outflow.
        # Wind FROM south (180°) → cos(180) = -1.0 → maximum inflow.
        north_component = np.cos(np.radians(wind_direction_deg))

        # Negative sign: north wind → water drops.
        setdown = -self.coeff * wind_speed_mph ** 2 * north_component
        return float(setdown)

    def calculate_setdown_timeseries(
        self,
        wind_forecasts: List[Dict],
    ) -> List[Dict]:
        """
        Calculate setdown for each hour in the wind forecast.

        Returns list of {"time": datetime, "setdown_ft": float}
        """
        results = []
        for wf in wind_forecasts:
            sd = self.calculate_setdown_ft(
                wf["wind_speed_mph"],
                wf["wind_direction_deg"],
            )
            results.append({
                "time": wf["time"],
                "setdown_ft": sd,
            })
        return results
