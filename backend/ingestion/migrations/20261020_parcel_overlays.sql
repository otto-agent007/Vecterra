-- =============================================================================
-- 2026-10-20 · Parcel overlay facts
--
-- Adds the per-parcel overlay columns that agents/rule_layers.py already reads
-- (fire zone, fire responsibility, fire agency, sewer/septic, HOA) plus the two
-- other jurisdictions inside the study-area bbox (52 San Marcos parcels and a
-- handful in the City of San Diego). Additive and idempotent; the same
-- statements are folded into ingestion/schema.sql for fresh databases.
--
-- Filled by: python -m ingestion.load_study_area
-- =============================================================================
set search_path = public, extensions;

alter type zoning_jurisdiction add value if not exists 'city_of_san_marcos';
alter type zoning_jurisdiction add value if not exists 'city_of_san_diego';

alter table parcels
  -- CAL FIRE Fire Hazard Severity Zone covering the largest share of the lot
  -- (SRA map effective 2024-04-01; LRA map released 2025-03-24). The share of
  -- every class, and the highest class touching >= 5% of the lot, live in overlay_facts.
  add column if not exists fire_hazard_zone    text check (fire_hazard_zone in ('Very High', 'High', 'Moderate', 'None')),
  add column if not exists fire_responsibility text check (fire_responsibility in ('SRA', 'LRA')),
  add column if not exists fire_agency         text,                 -- display name; null when the map shows no agency
  -- County DEH septic/sewer designation; null when the County lists it as unknown or has no record
  add column if not exists sewer_service       text check (sewer_service in ('sewer', 'septic')),
  -- true = likely in a common-interest development; null = not checked / no evidence (never "no HOA")
  add column if not exists hoa                 boolean,
  add column if not exists hoa_name            text,                 -- only when confirmed from a filing or the owner
  add column if not exists hoa_basis           text,                 -- e.g. 'common_lot_in_subdivision', 'confirmed'
  add column if not exists overlay_facts       jsonb not null default '{}'::jsonb,  -- shares, raw values, sources
  add column if not exists overlays_as_of      date;

create index if not exists parcels_fire_zone_idx on parcels (fire_hazard_zone);
create index if not exists parcels_sewer_idx     on parcels (sewer_service);
create index if not exists parcels_jurisdiction_idx on parcels (jurisdiction);

-- Overlay facts are property facts, so agents may read them alongside the
-- other public-record columns. Owner PII and tax_data stay service-role only.
grant select (
  fire_hazard_zone, fire_responsibility, fire_agency, sewer_service,
  hoa, hoa_name, hoa_basis, overlay_facts, overlays_as_of
) on parcels to authenticated;
