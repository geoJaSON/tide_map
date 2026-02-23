-- Depth Forecast schema for Supabase
-- Run this in the Supabase SQL Editor (https://supabase.com/dashboard)

-- 1. Metadata table (one row per date + hour snapshot)
CREATE TABLE IF NOT EXISTS depth_forecasts (
    id SERIAL PRIMARY KEY,
    forecast_date DATE NOT NULL,
    forecast_hour INT NOT NULL,          -- 7, 12, 17 (24h local time)
    image_url TEXT NOT NULL,
    bounds JSONB NOT NULL,               -- {"south": 29.0, "west": -91.0, "north": 29.3, "east": -90.3}
    effective_level_ft FLOAT,            -- tide + setdown at this hour
    tide_ft FLOAT,
    setdown_ft FLOAT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(forecast_date, forecast_hour)
);

CREATE INDEX IF NOT EXISTS idx_depth_forecast_date ON depth_forecasts(forecast_date);

-- 2. Migration from old schema (if upgrading from single-image-per-day):
--
-- ALTER TABLE depth_forecasts DROP CONSTRAINT IF EXISTS depth_forecasts_forecast_date_key;
-- ALTER TABLE depth_forecasts ADD COLUMN IF NOT EXISTS forecast_hour INT DEFAULT 12;
-- ALTER TABLE depth_forecasts RENAME COLUMN effective_low_ft TO effective_level_ft;
-- ALTER TABLE depth_forecasts DROP COLUMN IF EXISTS worst_hour;
-- ALTER TABLE depth_forecasts ADD UNIQUE(forecast_date, forecast_hour);

-- 3. Storage bucket (run via Supabase dashboard or API)
-- Create a public bucket called "depth-forecasts" in Storage settings.
-- The push_forecast.py script will create it automatically if it doesn't exist.
