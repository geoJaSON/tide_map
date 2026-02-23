"""
NOAA CO-OPS API client for tide predictions and water level observations.

API docs: https://api.tidesandcurrents.noaa.gov/api/prod/
"""

import json
import time
import requests
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional

NOAA_API = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"


class TideClient:
    """Fetches tide data from NOAA CO-OPS stations."""

    def __init__(self, cache_dir: str = "data/cache"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get_predictions(
        self,
        station_id: str,
        start_date: datetime,
        num_days: int = 7,
        interval_minutes: int = 60,
    ) -> List[Dict]:
        """
        Fetch tide predictions for a station.

        Returns list of {"time": datetime, "height_ft": float}
        where height is predicted water level above MLLW in feet.
        """
        end_date = start_date + timedelta(days=num_days)

        params = {
            "station": station_id,
            "begin_date": start_date.strftime("%Y%m%d"),
            "end_date": end_date.strftime("%Y%m%d"),
            "product": "predictions",
            "datum": "MLLW",
            "units": "english",
            "time_zone": "lst_ldt",
            "format": "json",
            "interval": str(interval_minutes),
            "application": "tide_map",
        }

        cache_key = f"pred_{station_id}_{params['begin_date']}_{params['end_date']}.json"
        cached = self._read_cache(cache_key)
        if cached is not None:
            return cached

        data = self._api_request(params)
        if data is None:
            return []

        predictions = []
        for entry in data.get("predictions", []):
            try:
                predictions.append({
                    "time": datetime.strptime(entry["t"], "%Y-%m-%d %H:%M"),
                    "height_ft": float(entry["v"]),
                })
            except (ValueError, KeyError):
                continue

        self._write_cache(cache_key, predictions)
        return predictions

    def get_multi_station_predictions(
        self,
        station_ids: List[str],
        start_date: datetime,
        num_days: int = 7,
        interval_minutes: int = 60,
    ) -> Dict[str, List[Dict]]:
        """
        Fetch tide predictions from multiple stations.

        Returns {station_id: [predictions]} for every station that
        successfully returned data.  Stations that fail are silently
        excluded so the caller can proceed with whatever is available.
        """
        results: Dict[str, List[Dict]] = {}
        for sid in station_ids:
            preds = self.get_predictions(sid, start_date, num_days, interval_minutes)
            if preds:
                results[sid] = preds
            else:
                print(f"  ⚠ Station {sid} returned no data — skipping")
        return results

    def get_observed_water_levels(
        self,
        station_id: str,
        start_date: datetime,
        end_date: datetime,
    ) -> List[Dict]:
        """
        Fetch observed (verified) water levels.
        Useful for building the empirical wind setdown model later.

        Returns list of {"time": datetime, "height_ft": float}
        """
        params = {
            "station": station_id,
            "begin_date": start_date.strftime("%Y%m%d"),
            "end_date": end_date.strftime("%Y%m%d"),
            "product": "water_level",
            "datum": "MLLW",
            "units": "english",
            "time_zone": "gmt",
            "format": "json",
            "application": "tide_map",
        }

        data = self._api_request(params)
        if data is None:
            return []

        levels = []
        for entry in data.get("data", []):
            try:
                levels.append({
                    "time": datetime.strptime(entry["t"], "%Y-%m-%d %H:%M"),
                    "height_ft": float(entry["v"]),
                })
            except (ValueError, KeyError):
                continue
        return levels

    def _api_request(self, params: dict, retries: int = 3) -> Optional[dict]:
        """Make an API request with retries and exponential backoff."""
        for attempt in range(retries):
            try:
                resp = requests.get(NOAA_API, params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                # NOAA returns errors inside the JSON body
                if "error" in data:
                    print(f"  NOAA API error: {data['error'].get('message', data['error'])}")
                    return None
                return data
            except requests.RequestException as e:
                if attempt < retries - 1:
                    wait = 2 ** attempt
                    print(f"  NOAA API request failed ({e}), retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"  NOAA API request failed after {retries} attempts: {e}")
                    return None

    def _read_cache(self, key: str) -> Optional[List]:
        path = self.cache_dir / key
        if not path.exists():
            return None
        # Cache valid for 6 hours
        age = time.time() - path.stat().st_mtime
        if age > 6 * 3600:
            return None
        try:
            with open(path, "r") as f:
                raw = json.load(f)
            # Deserialize datetimes
            return [
                {"time": datetime.fromisoformat(r["time"]), "height_ft": r["height_ft"]}
                for r in raw
            ]
        except (json.JSONDecodeError, KeyError):
            return None

    def _write_cache(self, key: str, data: List[Dict]) -> None:
        path = self.cache_dir / key
        try:
            serializable = [
                {"time": d["time"].isoformat(), "height_ft": d["height_ft"]}
                for d in data
            ]
            with open(path, "w") as f:
                json.dump(serializable, f)
        except OSError:
            pass  # Non-critical — just skip caching
