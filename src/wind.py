"""
NWS Weather API client for hourly wind forecasts.

API docs: https://www.weather.gov/documentation/services-web-api
"""

import time
import requests
from datetime import datetime
from typing import List, Dict, Optional

NWS_API = "https://api.weather.gov"
USER_AGENT = "(tide_map, oyster-survey-depth-tool)"

# Cardinal direction to degrees lookup
CARDINAL_TO_DEG = {
    "N": 0, "NNE": 22.5, "NE": 45, "ENE": 67.5,
    "E": 90, "ESE": 112.5, "SE": 135, "SSE": 157.5,
    "S": 180, "SSW": 202.5, "SW": 225, "WSW": 247.5,
    "W": 270, "WNW": 292.5, "NW": 315, "NNW": 337.5,
}


class WindForecastClient:
    """Fetches hourly wind forecasts from the NWS API."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self._grid_cache: Dict[str, dict] = {}

    def get_hourly_wind_forecast(
        self, lat: float, lon: float
    ) -> List[Dict]:
        """
        Fetch hourly wind forecast for a location (~7 days out).

        Returns list of:
        {
            "time": datetime,
            "wind_speed_mph": float,
            "wind_direction": str,       # e.g. "N", "SSE"
            "wind_direction_deg": float,  # 0-360
        }
        """
        grid = self._get_grid(lat, lon)
        if grid is None:
            return []

        forecast_url = grid["forecast_hourly_url"]
        data = self._api_request(forecast_url)
        if data is None:
            return []

        forecasts = []
        for period in data.get("properties", {}).get("periods", []):
            wind_speed = self._parse_wind_speed(period.get("windSpeed", "0 mph"))
            wind_dir = period.get("windDirection", "N")

            # Parse the ISO 8601 start time
            start_str = period["startTime"]
            try:
                t = datetime.fromisoformat(start_str)
                # Convert to naive local time for consistency with tide data
                t = t.replace(tzinfo=None)
            except ValueError:
                continue

            forecasts.append({
                "time": t,
                "wind_speed_mph": wind_speed,
                "wind_direction": wind_dir,
                "wind_direction_deg": CARDINAL_TO_DEG.get(wind_dir.upper(), 0),
            })

        return forecasts

    def _get_grid(self, lat: float, lon: float) -> Optional[dict]:
        """Resolve lat/lon to NWS grid coordinates."""
        key = f"{lat},{lon}"
        if key in self._grid_cache:
            return self._grid_cache[key]

        url = f"{NWS_API}/points/{lat},{lon}"
        data = self._api_request(url)
        if data is None:
            return None

        props = data.get("properties", {})
        grid = {
            "office": props.get("gridId"),
            "grid_x": props.get("gridX"),
            "grid_y": props.get("gridY"),
            "forecast_hourly_url": props.get("forecastHourly"),
        }

        if not grid["forecast_hourly_url"]:
            print("  NWS API: could not resolve grid for this location")
            return None

        self._grid_cache[key] = grid
        return grid

    def _api_request(self, url: str, retries: int = 3) -> Optional[dict]:
        """Make an API request with retries."""
        for attempt in range(retries):
            try:
                resp = self.session.get(url, timeout=30)
                if resp.status_code == 500:
                    # NWS API sometimes returns 500; retry
                    if attempt < retries - 1:
                        wait = 2 ** attempt
                        print(f"  NWS API returned 500, retrying in {wait}s...")
                        time.sleep(wait)
                        continue
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as e:
                if attempt < retries - 1:
                    wait = 2 ** attempt
                    print(f"  NWS API request failed ({e}), retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"  NWS API request failed after {retries} attempts: {e}")
                    return None

    @staticmethod
    def _parse_wind_speed(speed_str: str) -> float:
        """
        Parse NWS wind speed strings.
        Examples: "10 mph", "5 to 10 mph", "15 to 20 mph"
        Returns the higher value (conservative for setdown calculation).
        """
        cleaned = speed_str.lower().replace("mph", "").replace("knots", "").strip()
        parts = cleaned.split(" to ")
        try:
            return max(float(p.strip()) for p in parts)
        except ValueError:
            return 0.0
