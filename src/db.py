"""
Supabase client for uploading depth forecast images and metadata.

Uploads PNG overlays to Supabase Storage and upserts metadata
to the depth_forecasts table.
"""

import os
from datetime import date, datetime, timedelta
from typing import Dict, Optional

from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

BUCKET = "depth-forecasts"


def _get_client() -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return create_client(url, key)


def ensure_bucket(client: Client):
    """Create the storage bucket if it doesn't exist."""
    try:
        client.storage.get_bucket(BUCKET)
    except Exception:
        client.storage.create_bucket(
            BUCKET,
            options={"public": True},
        )
        print(f"  Created storage bucket: {BUCKET}")


def upload_image(
    client: Client,
    forecast_date: date,
    forecast_hour: int,
    png_bytes: bytes,
) -> str:
    """
    Upload a PNG to Supabase Storage.

    Filename format: 2026-02-23_07.png, 2026-02-23_12.png, etc.
    Returns the public URL for the image.
    """
    filename = f"{forecast_date.isoformat()}_{forecast_hour:02d}.png"

    # Remove existing file if present (upsert)
    try:
        client.storage.from_(BUCKET).remove([filename])
    except Exception:
        pass

    client.storage.from_(BUCKET).upload(
        filename,
        png_bytes,
        file_options={"content-type": "image/png", "upsert": "true"},
    )

    public_url = client.storage.from_(BUCKET).get_public_url(filename)
    return public_url


def upsert_forecast(
    client: Client,
    forecast_date: date,
    forecast_hour: int,
    image_url: str,
    bounds: Dict,
    effective_level_ft: float,
    tide_ft: float,
    setdown_ft: float,
):
    """Upsert a row in the depth_forecasts table."""
    row = {
        "forecast_date": forecast_date.isoformat(),
        "forecast_hour": forecast_hour,
        "image_url": image_url,
        "bounds": bounds,
        "effective_level_ft": round(effective_level_ft, 2),
        "tide_ft": round(tide_ft, 2),
        "setdown_ft": round(setdown_ft, 2),
        "updated_at": datetime.utcnow().isoformat(),
    }

    client.table("depth_forecasts").upsert(
        row, on_conflict="forecast_date,forecast_hour"
    ).execute()


def delete_old_forecasts(client: Client, keep_days: int = 14):
    """Delete forecast rows and images older than keep_days."""
    cutoff = (date.today() - timedelta(days=keep_days)).isoformat()

    # Get old rows to find their image filenames
    result = (
        client.table("depth_forecasts")
        .select("forecast_date, forecast_hour")
        .lt("forecast_date", cutoff)
        .execute()
    )

    old_rows = result.data or []
    if not old_rows:
        return

    # Delete images from storage
    filenames = [
        f"{r['forecast_date']}_{r['forecast_hour']:02d}.png"
        for r in old_rows
    ]
    try:
        client.storage.from_(BUCKET).remove(filenames)
    except Exception:
        pass

    # Delete rows
    client.table("depth_forecasts").delete().lt(
        "forecast_date", cutoff
    ).execute()

    n_dates = len(set(r["forecast_date"] for r in old_rows))
    print(f"  Cleaned up {n_dates} old forecast day(s) ({len(old_rows)} snapshots)")
