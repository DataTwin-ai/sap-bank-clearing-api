-- Extra active_bt3h columns read by the AR Inbox invoice query and written by
-- sync_bsid_to_bt3i.py / the ingestion service (context IDs). Re-runnable.
-- Already applied on the dev DB dm00005 (2026-11-20).
--   psql -h <db-host> -p <port> -U postgres -d dm00005 -f deploy/sql/bt3h_columns.sql

ALTER TABLE public.active_bt3h
  ADD COLUMN IF NOT EXISTS grouptransactionref text,
  ADD COLUMN IF NOT EXISTS basevalue double precision,
  ADD COLUMN IF NOT EXISTS documentvalue double precision,
  ADD COLUMN IF NOT EXISTS tdsvalue double precision,
  ADD COLUMN IF NOT EXISTS adjustedamount double precision;
