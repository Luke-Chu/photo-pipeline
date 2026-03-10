-- Photo Pipeline PostgreSQL schema bootstrap
-- Usage (from project root):
--   docker exec -i postgres-luke-chu psql -U admin -d postgres -f - < docs/postgresql_schema.sql
--
-- Notes:
-- 1) This script uses psql meta commands (\gexec, \connect).
-- 2) Database name contains '-', so double quotes are required.

-- Create target database if it does not exist.
SELECT format('CREATE DATABASE %I', 'luke-chu-site')
WHERE NOT EXISTS (
  SELECT 1 FROM pg_database WHERE datname = 'luke-chu-site'
)\gexec

\connect "luke-chu-site"

-- Optional: align database timezone with Asia/Shanghai.
ALTER DATABASE "luke-chu-site" SET timezone TO 'Asia/Shanghai';

-- Optional: if user 'admin' exists, set role-level default timezone in this database.
ALTER ROLE admin IN DATABASE "luke-chu-site" SET timezone TO 'Asia/Shanghai';

-- For title search index (GIN + trigram)
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TABLE IF NOT EXISTS photos (
  id BIGSERIAL PRIMARY KEY,
  uuid UUID UNIQUE,
  filename TEXT NOT NULL,
  title_cn VARCHAR(255),
  title_en VARCHAR(255),
  description TEXT,
  category VARCHAR(100),
  shot_time TIMESTAMP,
  year INTEGER,
  month INTEGER,
  day INTEGER,
  hour INTEGER,
  minute INTEGER,
  second INTEGER,
  width INTEGER,
  height INTEGER,
  orientation VARCHAR(20),
  resolution VARCHAR(50),
  camera_model TEXT,
  lens_model TEXT,
  aperture VARCHAR(50),
  shutter_speed VARCHAR(50),
  exposure_compensation VARCHAR(50),
  iso INTEGER,
  focal_length NUMERIC(8,2),
  focal_length_35mm NUMERIC(8,2),
  metering_mode VARCHAR(100),
  exposure_program VARCHAR(100),
  white_balance VARCHAR(100),
  flash VARCHAR(100),
  author VARCHAR(255),
  raw_exif JSONB NOT NULL DEFAULT '{}'::jsonb,
  ai_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  extra_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  thumb_url TEXT,
  display_url TEXT,
  original_url TEXT,
  like_count INTEGER NOT NULL DEFAULT 0,
  download_count INTEGER NOT NULL DEFAULT 0,
  view_count INTEGER NOT NULL DEFAULT 0,
  is_published BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DROP TRIGGER IF EXISTS trg_photos_updated_at ON photos;
CREATE TRIGGER trg_photos_updated_at
BEFORE UPDATE ON photos
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS tags (
  id BIGSERIAL PRIMARY KEY,
  name VARCHAR(100) NOT NULL,
  tag_type VARCHAR(20) NOT NULL CHECK (tag_type IN ('subject', 'element', 'mood')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_tags_name UNIQUE (name)
);

CREATE TABLE IF NOT EXISTS photo_tags (
  photo_id BIGINT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
  tag_id BIGINT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY (photo_id, tag_id)
);

CREATE TABLE IF NOT EXISTS photo_likes (
  id BIGSERIAL PRIMARY KEY,
  photo_id BIGINT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
  visitor_hash TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_photo_likes_photo_visitor UNIQUE (photo_id, visitor_hash)
);

CREATE INDEX IF NOT EXISTS idx_photos_shot_time_desc ON photos (shot_time DESC);
CREATE INDEX IF NOT EXISTS idx_photos_category ON photos (category);
CREATE INDEX IF NOT EXISTS idx_photos_year ON photos (year);
CREATE INDEX IF NOT EXISTS idx_photos_like_count_desc ON photos (like_count DESC);
CREATE INDEX IF NOT EXISTS idx_photos_download_count_desc ON photos (download_count DESC);
CREATE INDEX IF NOT EXISTS idx_photos_view_count_desc ON photos (view_count DESC);
CREATE INDEX IF NOT EXISTS idx_photos_title_cn_trgm ON photos USING GIN (title_cn gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_tags_name ON tags (name);
CREATE INDEX IF NOT EXISTS idx_photo_tags_tag_id ON photo_tags (tag_id);
