"""
Folium map generator for navigable depth forecasts.

Produces a single interactive HTML file with a layer per snapshot
(3 per day: 7 AM, 12 PM, 5 PM), color-coded by depth: red (no-go),
yellow (caution), green (safe), gray (no data).
"""

import numpy as np
import folium
import folium.raster_layers
from typing import List, Dict
from io import BytesIO
from PIL import Image
import base64


def depth_grid_to_png(
    depth_grid: np.ndarray,
    no_go: float = 3.0,
    caution: float = 4.0,
    max_width: int = 2000,
) -> bytes:
    """
    Convert a depth grid (feet) to PNG bytes.

    Color scheme:
        Red   (255, 50, 50, 180)  — depth < no_go
        Yellow(255, 255, 0, 180)  — no_go ≤ depth < caution
        Green (50, 200, 50, 180)  — depth ≥ caution
        Gray  (128, 128, 128, 80) — NaN (no survey data)

    Large grids are downsampled to max_width using nearest-neighbor
    on the classified image (preserves color boundaries).

    Returns raw PNG bytes.
    """
    rows, cols = depth_grid.shape
    rgba = np.zeros((rows, cols, 4), dtype=np.uint8)

    rgba[depth_grid < no_go] = [255, 50, 50, 180]
    rgba[(depth_grid >= no_go) & (depth_grid < caution)] = [255, 255, 0, 180]
    rgba[depth_grid >= caution] = [50, 200, 50, 180]
    rgba[np.isnan(depth_grid)] = [128, 128, 128, 80]

    img = Image.fromarray(rgba, "RGBA")

    if cols > max_width:
        scale = max_width / cols
        img = img.resize((max_width, max(1, int(rows * scale))), Image.NEAREST)

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


class MapBuilder:
    """Generates color-coded navigable depth maps."""

    NO_GO_DEPTH = 3.0    # feet — red
    CAUTION_DEPTH = 4.0   # feet — yellow
    MAX_OVERLAY_WIDTH = 2000  # pixels — downsample large grids for performance

    def __init__(self, center: list, zoom: int = 11):
        self.center = center
        self.zoom = zoom

    def build_snapshot_map(
        self,
        snapshots: List[Dict],
        grid_bounds: list,
    ) -> folium.Map:
        """
        Build a Folium map with one toggleable layer per snapshot.

        Args:
            snapshots:   From DepthCalculator.compute_snapshots
            grid_bounds: [[south, west], [north, east]]

        Returns:
            folium.Map ready to save as HTML.
        """
        m = folium.Map(
            location=self.center,
            zoom_start=self.zoom,
            tiles=None,
        )

        # Base layers
        folium.TileLayer("OpenStreetMap", name="Street Map").add_to(m)
        folium.TileLayer(
            tiles=(
                "https://server.arcgisonline.com/ArcGIS/rest/services/"
                "World_Imagery/MapServer/tile/{z}/{y}/{x}"
            ),
            attr="Esri",
            name="Satellite",
        ).add_to(m)

        # One layer per snapshot
        for i, snap in enumerate(snapshots):
            date_str = snap["date"].strftime("%a %m/%d")
            time_str = snap["time"].strftime("%I %p").lstrip("0")
            eff_level = snap["tide_ft"] + snap["setdown_ft"]

            layer_name = (
                f"{date_str} {time_str}  |  {eff_level:+.1f} ft"
            )

            fg = folium.FeatureGroup(
                name=layer_name,
                show=(i == 0),  # Show first snapshot by default
            )

            # Convert depth grid to a PNG and overlay
            png_bytes = depth_grid_to_png(
                snap["depth_grid"],
                self.NO_GO_DEPTH, self.CAUTION_DEPTH, self.MAX_OVERLAY_WIDTH,
            )
            png_b64 = base64.b64encode(png_bytes).decode("ascii")

            folium.raster_layers.ImageOverlay(
                image=f"data:image/png;base64,{png_b64}",
                bounds=grid_bounds,
                opacity=0.55,
                name=f"{date_str} {time_str}",
            ).add_to(fg)

            # Conditions marker
            self._add_snapshot_marker(fg, snap)

            fg.add_to(m)

        folium.LayerControl(collapsed=False).add_to(m)
        self._add_legend(m)

        return m

    def _add_snapshot_marker(self, fg, snap):
        """Add a popup marker showing conditions for a snapshot."""
        eff_level = snap["tide_ft"] + snap["setdown_ft"]
        popup_html = (
            f"<div style='font-family:Arial; font-size:13px; min-width:200px;'>"
            f"<b>{snap['date'].strftime('%A %m/%d/%Y')}</b><br>"
            f"<b>{snap['time'].strftime('%I:%M %p')}</b><br><br>"
            f"Tide: {snap['tide_ft']:+.1f} ft MLLW<br>"
            f"Wind setdown: {snap['setdown_ft']:+.1f} ft<br>"
            f"<b>Effective level: {eff_level:+.1f} ft MLLW</b><br>"
            f"</div>"
        )
        folium.Marker(
            location=self.center,
            popup=folium.Popup(popup_html, max_width=300),
            icon=folium.Icon(icon="info-sign", color="blue"),
        ).add_to(fg)

    def _add_legend(self, m: folium.Map):
        """Add a fixed HTML legend to the map."""
        legend_html = """
        <div style="
            position: fixed;
            bottom: 30px; left: 30px;
            background: white;
            padding: 12px 16px;
            border-radius: 6px;
            border: 2px solid #666;
            font-family: Arial, sans-serif;
            font-size: 13px;
            z-index: 9999;
            box-shadow: 2px 2px 6px rgba(0,0,0,0.3);
        ">
            <b style="font-size:14px;">Predicted Depth</b><br><br>
            <span style="background:#32C832;width:16px;height:16px;
               display:inline-block;border:1px solid #333;"></span>
            &nbsp; &gt; 4 ft &mdash; Safe<br>
            <span style="background:#FFFF00;width:16px;height:16px;
               display:inline-block;border:1px solid #333;"></span>
            &nbsp; 3&ndash;4 ft &mdash; Caution<br>
            <span style="background:#FF3232;width:16px;height:16px;
               display:inline-block;border:1px solid #333;"></span>
            &nbsp; &lt; 3 ft &mdash; No-Go<br>
            <span style="background:#808080;width:16px;height:16px;
               display:inline-block;border:1px solid #333;"></span>
            &nbsp; No Survey Data<br>
        </div>
        """
        m.get_root().html.add_child(folium.Element(legend_html))

    @staticmethod
    def save(m: folium.Map, filepath: str):
        m.save(filepath)
