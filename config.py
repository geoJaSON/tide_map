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
    "8762075": {"name": "Port Fourchon, Belle Pass",    "lat": 29.1142, "lon": -90.1993, "vdatum_offset": -0.63},
    # Barataria / Plaquemines
    "8761724": {"name": "Grand Isle",                   "lat": 29.2633, "lon": -89.9572, "vdatum_offset": -0.55},
    "8760721": {"name": "Pilottown",                    "lat": 29.1793, "lon": -89.2585, "vdatum_offset": -0.45},
    "8760922": {"name": "Pilots Station East, SW Pass", "lat": 28.9322, "lon": -89.4075, "vdatum_offset": -0.40},
    # St. Bernard / Breton Sound
    "8761305": {"name": "Shell Beach",                  "lat": 29.8683, "lon": -89.6731, "vdatum_offset": -0.35},
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

# For backward compatibility, keep comments mostly the same but remove constant.
# We now dynamically build a VDatum offset grid interpolating per-station offsets.
# NAVD88_TO_MLLW_OFFSET_FT is no longer used.

# ---------------------------------------------------------------------------
# Depth thresholds (feet)
# ---------------------------------------------------------------------------
MIN_NAVIGABLE_DEPTH_FT = 2.0   # Red below this — cannot navigate
CAUTION_DEPTH_FT = 4.0         # Yellow between MIN and this — use caution

# ---------------------------------------------------------------------------
# Snapshot hours — depth grids are generated at these times each day.
# Uses 24-hour local time.
# ---------------------------------------------------------------------------
SNAPSHOT_HOURS = [7, 12, 17]   # 7 AM, 12 PM, 5 PM

# ---------------------------------------------------------------------------
# Wind setup/setdown model (USACE wind stress)
# ---------------------------------------------------------------------------
# Uses a 4-band fetch raster (N, E, S, W) with S = Cd × U² × F / (g × d)
# to compute spatially varying wind-driven water level changes.
# avg_depth_ft is the representative average basin depth.
# Drag coefficient (Cd = 2.5e-6) is set in WindSetupModel.DRAG_COEFF.
FETCH_RASTER_PATH = "data/fetch/fetch_4dir.tif"
SIBUL_AVG_DEPTH_FT = 5.0

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
