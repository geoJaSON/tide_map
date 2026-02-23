-- Depth Forecast schema for Supabase
-- Run this in the Supabase SQL Editor (https://supabase.com/dashboard)

-- 1. Metadata table
CREATE TABLE IF NOT EXISTS depth_forecasts (
    id SERIAL PRIMARY KEY,
    forecast_date DATE NOT NULL UNIQUE,
    image_url TEXT NOT NULL,
    bounds JSONB NOT NULL,           -- {"south": 29.0, "west": -91.0, "north": 29.3, "east": -90.3}
    effective_low_ft FLOAT,
    tide_ft FLOAT,
    setdown_ft FLOAT,
    worst_hour TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_depth_forecast_date ON depth_forecasts(forecast_date);

-- 2. Storage bucket (run via Supabase dashboard or API)
-- Create a public bucket called "depth-forecasts" in Storage settings.
-- The push_forecast.py script will create it automatically if it doesn't exist.
