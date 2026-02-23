"""
Central configuration for South Louisiana Navigable Depth Predictor.

Adjust these values to match your area and calibrate against observations.
"""

# ---------------------------------------------------------------------------
# Geographic bounds  (Terrebonne Bay through Breton Sound)
# Covers the primary oyster-producing waters of coastal Louisiana.
# ---------------------------------------------------------------------------
BOUNDS = {
    "north": 30.17,
    "south": 28.97,
    "east": -89.09,
    "west": -91.32,
}

# ---------------------------------------------------------------------------
# NOAA CO-OPS tide stations
# Predictions are fetched from all stations, then spatially interpolated
# using Inverse Distance Weighting (IDW) to produce per-pixel tide grids.
# If a station fails to return data it is silently excluded from the IDW.
# ---------------------------------------------------------------------------
TIDE_STATIONS = {
    # Terrebonne / Caillou
    "8762075": {"name": "Port Fourchon, Belle Pass",    "lat": 29.1142, "lon": -90.1993},
    # Barataria / Plaquemines
    "8761724": {"name": "Grand Isle",                   "lat": 29.2633, "lon": -89.9572},
    "8760721": {"name": "Pilottown",                    "lat": 29.1793, "lon": -89.2585},
    "8760922": {"name": "Pilots Station East, SW Pass", "lat": 28.9322, "lon": -89.4075},
    # St. Bernard / Breton Sound
    "8761305": {"name": "Shell Beach",                  "lat": 29.8683, "lon": -89.6731},
}

# ---------------------------------------------------------------------------
# IDW (Inverse Distance Weighting) parameters for tide interpolation
# ---------------------------------------------------------------------------
IDW_POWER = 2          # Higher values -> closer stations dominate more
IDW_COARSE_RES = 300   # Max dimension of coarse interpolation grid

# ---------------------------------------------------------------------------
# Grid resolution limit
# ---------------------------------------------------------------------------
# BlueTopo native resolution (~1 m) produces enormous grids at this scale.
# Cap the longest dimension to keep memory reasonable.  The final PNG overlay
# is downsampled to 2000 px anyway, so 5000 px gives plenty of headroom.
MAX_GRID_DIM = 5000

# ---------------------------------------------------------------------------
# Datum conversion
# ---------------------------------------------------------------------------
# BlueTopo elevations are NAVD88 (meters, negative = underwater).
# NOAA tides are relative to MLLW (feet).
#
# At Port Fourchon the NAVD88-to-MLLW offset is approximately -0.63 ft,
# meaning NAVD88 zero sits about 0.63 ft ABOVE MLLW zero.
# This value varies slightly along the coast but is a reasonable average
# for the Louisiana bight.  Refine with NOAA VDatum if needed.
NAVD88_TO_MLLW_OFFSET_FT = -0.63

# ---------------------------------------------------------------------------
# Depth thresholds (feet)
# ---------------------------------------------------------------------------
MIN_NAVIGABLE_DEPTH_FT = 3.0   # Red below this — cannot navigate
CAUTION_DEPTH_FT = 4.0         # Yellow between MIN and this — use caution

# ---------------------------------------------------------------------------
# Working hours — only consider conditions during this window.
# Uses 24-hour local time.  Set to (0, 24) for round-the-clock.
# ---------------------------------------------------------------------------
WORK_HOURS = (5, 19)   # 5 AM – 7 PM

# ---------------------------------------------------------------------------
# Wind setdown model
# ---------------------------------------------------------------------------
# Empirical coefficient: feet of setdown per (mph)^2 of north-wind component.
# Calibrated so a 20 mph due-north wind produces ~1.2 ft of setdown.
#   1.2 / (20^2) = 0.003
# Increase if you observe more setdown than predicted; decrease if less.
SETDOWN_COEFF = 0.003

# ---------------------------------------------------------------------------
# NWS wind forecast location (center of work area)
# ---------------------------------------------------------------------------
WIND_FORECAST_LAT = 29.55
WIND_FORECAST_LON = -90.20

# ---------------------------------------------------------------------------
# Paths (relative to project root)
# ---------------------------------------------------------------------------
BLUETOPO_DIR = "data/bluetopo"
CACHE_DIR = "data/cache"
OUTPUT_DIR = "output"

# ---------------------------------------------------------------------------
# Map display
# ---------------------------------------------------------------------------
MAP_CENTER = [29.55, -90.20]
MAP_ZOOM = 9
