"""
BlueTopo bathymetry loader.

Supports two modes:
  1. VRT mosaic (preferred) — open a pre-built .vrt file (fast, no merging)
  2. Raw tiles fallback — glob .tiff/.tif files, reproject, merge in memory

Build the VRT first with:  python build_vrt.py
"""

import numpy as np
import rasterio
from rasterio.windows import from_bounds
from pathlib import Path
from typing import Optional


class BathymetryGrid:
    """Loads BlueTopo bathymetry and provides depth data for the work area."""

    def __init__(
        self,
        bluetopo_dir: str,
        bounds: dict,
        vrt_name: str = "mosaic.vrt",
        max_dim: int = 0,
    ):
        """
        Args:
            bluetopo_dir: Path to directory containing the VRT (or raw tiles).
            bounds:       Dict with north/south/east/west in decimal degrees.
            vrt_name:     Name of the VRT file to look for.
            max_dim:      If > 0, downsample so the longest grid dimension
                          does not exceed this value.  Keeps memory manageable
                          for large coverage areas.
        """
        self.bounds = bounds
        self.max_dim = max_dim
        self.elevation_m: Optional[np.ndarray] = None  # NAVD88 meters
        self.transform = None
        self.shape = (0, 0)

        vrt_path = Path(bluetopo_dir) / vrt_name
        if vrt_path.exists():
            self._load_from_vrt(str(vrt_path))
        else:
            # Fallback: try raw tiles
            self._load_from_tiles(bluetopo_dir)

    def _load_from_vrt(self, vrt_path: str):
        """Load bathymetry from a pre-built VRT mosaic (fast path)."""
        print(f"  Using VRT mosaic: {vrt_path}")
        src = rasterio.open(vrt_path)

        # Read only the window that covers our bounds
        window = from_bounds(
            self.bounds["west"],
            self.bounds["south"],
            self.bounds["east"],
            self.bounds["north"],
            transform=src.transform,
        )

        # Clamp window to the actual raster extent
        window = window.intersection(rasterio.windows.Window(0, 0, src.width, src.height))

        native_h = int(window.height)
        native_w = int(window.width)

        # Optionally downsample to keep memory manageable
        if self.max_dim > 0 and max(native_h, native_w) > self.max_dim:
            scale = self.max_dim / max(native_h, native_w)
            out_h = max(1, int(native_h * scale))
            out_w = max(1, int(native_w * scale))
            print(f"  Downsampling: {native_w}x{native_h} -> {out_w}x{out_h}  "
                  f"(max_dim={self.max_dim})")
            self.elevation_m = src.read(
                1, window=window, out_shape=(out_h, out_w),
                resampling=rasterio.enums.Resampling.average,
            ).astype(np.float32)
        else:
            self.elevation_m = src.read(1, window=window).astype(np.float32)

        # Compute transform that matches the actual output grid
        win_transform = src.window_transform(window)
        actual_h, actual_w = self.elevation_m.shape
        if actual_w != native_w or actual_h != native_h:
            from rasterio.transform import Affine
            sx = native_w / actual_w
            sy = native_h / actual_h
            self.transform = win_transform * Affine.scale(sx, sy)
        else:
            self.transform = win_transform

        self.shape = self.elevation_m.shape
        src.close()

    def _load_from_tiles(self, bluetopo_dir: str):
        """Fallback: glob for raw tiles, merge with rasterio."""
        from rasterio.merge import merge
        from rasterio.warp import calculate_default_transform, reproject, Resampling
        from rasterio.io import MemoryFile

        tif_files = sorted(Path(bluetopo_dir).glob("*.tiff"))
        tif_files += sorted(Path(bluetopo_dir).glob("*.tif"))

        # Also search one level deep (e.g., Louisiana_Bathy/)
        for subdir in Path(bluetopo_dir).iterdir():
            if subdir.is_dir():
                tif_files += sorted(subdir.glob("*.tiff"))
                tif_files += sorted(subdir.glob("*.tif"))

        tif_files = sorted(set(tif_files))

        if not tif_files:
            raise FileNotFoundError(
                f"No .tiff/.tif files or mosaic.vrt found in {bluetopo_dir}.\n"
                f"Run 'python build_vrt.py' first, or place tiles in the directory."
            )

        print(f"  No VRT found — merging {len(tif_files)} tile(s) in memory (slow)...")
        print(f"  Tip: run 'python build_vrt.py' to create a VRT for faster loading.")

        temp_files = []
        datasets = []
        for tif_path in tif_files:
            src = rasterio.open(str(tif_path))
            # Check if reprojection needed (look for EPSG:4326 in CRS)
            crs_str = str(src.crs) if src.crs else ""
            if "4326" not in crs_str:
                dst_crs = "EPSG:4326"
                transform, width, height = calculate_default_transform(
                    src.crs, dst_crs, src.width, src.height, *src.bounds
                )
                kwargs = src.meta.copy()
                kwargs.update({
                    "crs": dst_crs,
                    "transform": transform,
                    "width": width,
                    "height": height,
                    "count": 1,
                    "dtype": "float32",
                })
                memfile = MemoryFile()
                temp_files.append(memfile)
                dst = memfile.open(**kwargs)
                reproject(
                    source=rasterio.band(src, 1),
                    destination=rasterio.band(dst, 1),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=transform,
                    dst_crs=dst_crs,
                    resampling=Resampling.bilinear,
                )
                dst.close()
                src.close()
                datasets.append(memfile.open())
            else:
                datasets.append(src)

        mosaic, mosaic_transform = merge(
            datasets,
            bounds=(
                self.bounds["west"],
                self.bounds["south"],
                self.bounds["east"],
                self.bounds["north"],
            ),
            indexes=[1],
            nodata=np.nan,
        )

        self.elevation_m = mosaic[0]
        self.transform = mosaic_transform
        self.shape = self.elevation_m.shape

        for ds in datasets:
            ds.close()

    def get_static_depth_mllw_ft(self, navd88_to_mllw_offset) -> np.ndarray:
        """
        Convert NAVD88 elevation to depth of water at MLLW, in feet.

        BlueTopo convention: negative elevation = below NAVD88 = underwater.

        Conversion:
            elev_ft = elevation_m * 3.28084
            depth_below_mllw = -(elev_ft + offset)

        Args:
            navd88_to_mllw_offset: Float constant, or a 2D np.ndarray grid 
                                   of spatial VDatum offsets.

        Returns:
            2D array of water depth in feet at MLLW.
            Positive = water.  NaN = no survey data.
        """
        elev_ft = self.elevation_m * 3.28084
        depth = -(elev_ft + navd88_to_mllw_offset)
        return depth

    def get_grid_bounds_latlon(self):
        """Return [[south, west], [north, east]] for Folium ImageOverlay."""
        return [
            [self.bounds["south"], self.bounds["west"]],
            [self.bounds["north"], self.bounds["east"]],
        ]
