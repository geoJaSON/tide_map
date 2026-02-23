"""
Build a Virtual Raster (VRT) mosaic from BlueTopo tiles.

This is the GDAL equivalent of a mosaic dataset in ArcGIS Pro:
a lightweight XML file that references all your tiles and presents
them as a single raster — no data is copied or duplicated.

Handles mixed CRS tiles (e.g., UTM zone 15N and 16N) by warping
each tile to a common CRS, then mosaicking with BuildVRT.

Usage:
    python build_vrt.py
    python build_vrt.py --src data/bluetopo/Louisiana_Bathy
    python build_vrt.py --target-crs EPSG:26915
"""

import argparse
import glob
import os
import sys

from osgeo import gdal, osr

gdal.UseExceptions()


def _get_horizontal_epsg(ds):
    """
    Extract the horizontal EPSG code from a dataset's CRS.

    BlueTopo tiles use compound CRS like "NAD83 / UTM zone 15N + NAVD88".
    We need just the horizontal part (e.g., EPSG:26915) so that GDAL
    doesn't try to transform elevation values during reprojection.
    """
    srs = osr.SpatialReference(wkt=ds.GetProjection())

    # Check if it's a compound CRS
    if srs.IsCompound():
        # Extract the horizontal (PROJCS or GEOGCS) component
        horiz = srs.GetAttrValue("COMPD_CS|PROJCS|AUTHORITY", 1)
        if horiz:
            return f"EPSG:{horiz}"
        horiz = srs.GetAttrValue("COMPD_CS|GEOGCS|AUTHORITY", 1)
        if horiz:
            return f"EPSG:{horiz}"

    # Not compound — try to get EPSG directly
    srs.AutoIdentifyEPSG()
    code = srs.GetAuthorityCode(None)
    if code:
        return f"EPSG:{code}"

    # Fallback: return the WKT
    return srs.ExportToWkt()


def build_vrt(
    src_dir: str,
    output_vrt: str,
    target_crs: str = "EPSG:4326",
    band: int = 1,
):
    """
    Build a VRT mosaic from all .tiff/.tif files in src_dir.

    Strategy for mixed CRS:
      1. Warp each tile to target CRS as an individual .vrt (no data copy)
      2. BuildVRT all the warped .vrt files into a single mosaic.vrt

    Compound CRS (e.g., UTM + NAVD88) are stripped to their horizontal
    component before warping, so elevation values are not transformed.
    """
    patterns = ["*.tiff", "*.tif"]
    tiles = []
    for pat in patterns:
        tiles.extend(glob.glob(os.path.join(src_dir, pat)))
    tiles = sorted(set(tiles))

    if not tiles:
        print(f"ERROR: No .tiff/.tif files found in {src_dir}")
        sys.exit(1)

    print(f"Found {len(tiles)} tile(s) in {src_dir}")

    tgt_srs = osr.SpatialReference()
    tgt_srs.SetFromUserInput(target_crs)

    # Directory for intermediate per-tile warped VRTs
    vrt_dir = os.path.join(os.path.dirname(output_vrt) or ".", "_tile_vrts")
    os.makedirs(vrt_dir, exist_ok=True)

    warped_vrts = []
    needs_warp = 0
    for i, tile in enumerate(tiles):
        tile_name = os.path.splitext(os.path.basename(tile))[0]
        warped_path = os.path.join(vrt_dir, f"{tile_name}_b{band}_4326.vrt")

        ds = gdal.Open(tile)
        if ds is None:
            print(f"  WARNING: Could not open {tile}, skipping")
            continue

        # Get horizontal-only CRS (strip vertical datum from compound CRS)
        horiz_crs = _get_horizontal_epsg(ds)
        horiz_srs = osr.SpatialReference()
        horiz_srs.SetFromUserInput(horiz_crs)
        same_crs = horiz_srs.IsSame(tgt_srs)
        ds = None

        if same_crs:
            # Just extract the band, no reprojection needed
            opts = gdal.BuildVRTOptions(
                bandList=[band], srcNodata="nan", VRTNodata="nan"
            )
            vrt_ds = gdal.BuildVRT(warped_path, [tile], options=opts)
        else:
            # Warp this single tile to target CRS as a VRT
            needs_warp += 1
            # First extract just the band we want
            band_vrt = os.path.join(vrt_dir, f"{tile_name}_b{band}.vrt")
            opts = gdal.BuildVRTOptions(
                bandList=[band], srcNodata="nan", VRTNodata="nan"
            )
            gdal.BuildVRT(band_vrt, [tile], options=opts)

            # Warp with explicit horizontal-only source CRS to prevent
            # GDAL from applying vertical datum transformations
            warp_opts = gdal.WarpOptions(
                format="VRT",
                srcSRS=horiz_crs,
                dstSRS=target_crs,
                resampleAlg="bilinear",
                srcNodata="nan",
                dstNodata="nan",
            )
            vrt_ds = gdal.Warp(warped_path, [band_vrt], options=warp_opts)

        if vrt_ds is None:
            print(f"  WARNING: Failed to process {tile}, skipping")
            continue
        vrt_ds.FlushCache()
        vrt_ds = None
        warped_vrts.append(warped_path)

    if not warped_vrts:
        print("ERROR: No tiles could be processed")
        sys.exit(1)

    if needs_warp:
        print(f"  Reprojected {needs_warp} tile(s) to {target_crs}")

    # Build final mosaic VRT from all the warped per-tile VRTs
    print(f"  Building mosaic from {len(warped_vrts)} tile(s)...")
    mosaic_opts = gdal.BuildVRTOptions(
        srcNodata="nan",
        VRTNodata="nan",
    )
    mosaic_ds = gdal.BuildVRT(output_vrt, warped_vrts, options=mosaic_opts)
    if mosaic_ds is None:
        print("ERROR: Final BuildVRT failed")
        sys.exit(1)
    mosaic_ds.FlushCache()
    mosaic_ds = None

    print(f"\nVRT mosaic created: {output_vrt}")
    _print_vrt_info(output_vrt)
    print(f"\nIntermediate tile VRTs stored in: {vrt_dir}/")
    print("(Keep these — the mosaic.vrt references them.)")


def _print_vrt_info(vrt_path: str):
    ds = gdal.Open(vrt_path)
    if ds is None:
        return

    print(f"  Size:   {ds.RasterXSize} x {ds.RasterYSize}")
    print(f"  Bands:  {ds.RasterCount}")

    gt = ds.GetGeoTransform()
    west = gt[0]
    north = gt[3]
    east = west + gt[1] * ds.RasterXSize
    south = north + gt[5] * ds.RasterYSize
    print(f"  Bounds: W={west:.4f}  E={east:.4f}  S={south:.4f}  N={north:.4f}")
    print(f"  Pixel:  {abs(gt[1]):.6f} x {abs(gt[5]):.6f}")
    ds = None


def main():
    parser = argparse.ArgumentParser(
        description="Build a VRT mosaic from BlueTopo tiles"
    )
    parser.add_argument(
        "--src",
        default="data/bluetopo/Louisiana_Bathy",
        help="Directory containing .tiff tiles",
    )
    parser.add_argument(
        "--output",
        default="data/bluetopo/mosaic.vrt",
        help="Output VRT file path",
    )
    parser.add_argument(
        "--target-crs",
        default="EPSG:4326",
        help="Target CRS (default: EPSG:4326 for lat/lon)",
    )
    parser.add_argument(
        "--band",
        type=int,
        default=1,
        help="Band to extract (default: 1 = elevation)",
    )
    args = parser.parse_args()

    build_vrt(args.src, args.output, target_crs=args.target_crs, band=args.band)


if __name__ == "__main__":
    main()
