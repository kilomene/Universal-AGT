-- 002: artifact upload lifecycle status.
-- Additive change: the canonical schema's artifacts table has no status
-- column, but the API must mark uploads as failed when size/sha256
-- verification rejects them. Values: pending -> ready | failed.
ALTER TABLE artifacts
  ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'pending'
  CHECK (status IN ('pending', 'ready', 'failed'));
