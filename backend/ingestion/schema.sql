-- =============================================================================
-- Vecterra · Supabase / PostGIS schema
-- Target: Supabase Postgres 15+ with PostGIS.
-- Apply with: supabase db push, the SQL editor, or `psql $DATABASE_URL -f schema.sql`
-- Idempotent where practical so it can be re-run during early development.
-- =============================================================================

create extension if not exists postgis;
create extension if not exists pgcrypto;   -- gen_random_uuid()

-- -----------------------------------------------------------------------------
-- Enums
-- -----------------------------------------------------------------------------
do $$ begin
  create type zoning_jurisdiction as enum ('san_diego_county', 'city_of_escondido', 'unknown');
exception when duplicate_object then null; end $$;

do $$ begin
  create type scan_status as enum ('queued', 'uploaded', 'processing', 'scored', 'failed');
exception when duplicate_object then null; end $$;

do $$ begin
  create type dossier_status as enum ('draft', 'compliance_hold', 'approved', 'rejected');
exception when duplicate_object then null; end $$;

-- -----------------------------------------------------------------------------
-- updated_at trigger helper
-- -----------------------------------------------------------------------------
create or replace function set_updated_at() returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end $$;

-- =============================================================================
-- parcels — one row per assessor parcel (APN). Source: county GIS parcel layer
-- (e.g. SanGIS PARCELS) + assessor roll. No MLS data touches this table.
-- =============================================================================
create table if not exists parcels (
  id                          uuid primary key default gen_random_uuid(),
  apn                         text not null unique,                 -- e.g. 235-123-45-00
  situs_address               text,
  situs_city                  text,
  situs_zip                   text,

  -- Harmony Grove is unincorporated San Diego County with an Escondido mailing
  -- address, so jurisdiction must be derived from boundaries, not the city string.
  jurisdiction                zoning_jurisdiction not null default 'unknown',
  zoning_code                 text,                                 -- e.g. RR, RS, A70, R-1-7
  zoning_desc                 text,
  land_use_code               text,
  lot_area_sqft               numeric,
  year_built                  integer,
  living_area_sqft            numeric,
  owner_occupied              boolean,

  -- County tax / assessor data (public record)
  assessed_land_value         numeric,
  assessed_improvement_value  numeric,
  assessed_total_value        numeric generated always as
                                (coalesce(assessed_land_value, 0) + coalesce(assessed_improvement_value, 0)) stored,
  tax_year                    integer,
  last_sale_date              date,
  last_sale_price             numeric,
  tax_data                    jsonb not null default '{}'::jsonb,   -- raw assessor attributes, untouched

  -- Modeled values (NOT public record — always labeled as estimates downstream)
  estimated_market_value      numeric,
  estimated_equity            numeric,
  equity_confidence           numeric check (equity_confidence between 0 and 1),
  equity_model_version        text,

  -- Owner contact fields are nullable and ingested only with --include-owner.
  owner_name                  text,
  owner_mailing_address       text,

  -- Geometry (WGS84). Multipolygon parcels are reduced to their largest ring at ingest.
  geom                        geometry(Polygon, 4326) not null,
  centroid                    geometry(Point, 4326) generated always as (st_centroid(geom)) stored,

  source                      text,                                 -- e.g. 'sangis_parcels_2026q3'
  source_ingested_at          timestamptz,
  created_at                  timestamptz not null default now(),
  updated_at                  timestamptz not null default now()
);

create index if not exists parcels_geom_gix      on parcels using gist (geom);
create index if not exists parcels_centroid_gix  on parcels using gist (centroid);
create index if not exists parcels_zip_idx       on parcels (situs_zip);
create index if not exists parcels_zoning_idx    on parcels (zoning_code);
create index if not exists parcels_equity_idx    on parcels (estimated_equity desc nulls last);

drop trigger if exists parcels_set_updated_at on parcels;
create trigger parcels_set_updated_at before update on parcels
  for each row execute function set_updated_at();

-- =============================================================================
-- property_scans — one row per field capture session (photos + optional LiDAR)
-- =============================================================================
create table if not exists property_scans (
  id                 uuid primary key default gen_random_uuid(),   -- generated on device (offline-first)
  parcel_id          uuid references parcels(id) on delete cascade,
  apn                text not null,                                 -- denormalized so a scan can be queued before parcel sync
  captured_by        uuid references auth.users(id) on delete set null,
  device_id          text,
  captured_at        timestamptz not null default now(),
  uploaded_at        timestamptz,
  status             scan_status not null default 'queued',

  capture_location   geometry(Point, 4326),                         -- where the phone stood
  heading_deg        numeric,                                       -- compass heading toward the structure

  -- Supabase Storage object paths inside bucket `property-scans`
  -- e.g. '235-123-45-00/<scan_id>/photo-1.jpg'
  image_paths        text[] not null default '{}',
  lidar_path         text,                                          -- optional mesh/point-cloud object path

  -- Raw on-device CV output: blur/exposure scores, facade bounding boxes, EXIF, etc.
  edge_cv_metadata   jsonb not null default '{}'::jsonb,
  device_metadata    jsonb not null default '{}'::jsonb,

  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);

create index if not exists property_scans_parcel_idx   on property_scans (parcel_id);
create index if not exists property_scans_apn_idx      on property_scans (apn);
create index if not exists property_scans_status_idx   on property_scans (status);
create index if not exists property_scans_location_gix on property_scans using gist (capture_location);

drop trigger if exists property_scans_set_updated_at on property_scans;
create trigger property_scans_set_updated_at before update on property_scans
  for each row execute function set_updated_at();

-- Backfill parcel_id from APN when a scan arrives before the client resolved it.
create or replace function property_scans_resolve_parcel() returns trigger language plpgsql as $$
begin
  if new.parcel_id is null then
    select id into new.parcel_id from parcels where apn = new.apn;
  end if;
  return new;
end $$;

drop trigger if exists property_scans_resolve_parcel on property_scans;
create trigger property_scans_resolve_parcel before insert on property_scans
  for each row execute function property_scans_resolve_parcel();

-- =============================================================================
-- dossiers — AI-generated property dossier + compliance audit trail
-- =============================================================================
create table if not exists dossiers (
  id                 uuid primary key default gen_random_uuid(),
  parcel_id          uuid not null references parcels(id) on delete cascade,
  scan_id            uuid references property_scans(id) on delete set null,
  version            integer not null default 1,
  status             dossier_status not null default 'draft',

  -- Structured outputs from the agent pipeline
  health_scores      jsonb not null,                                -- {roof:{score,confidence,...}, paint:{}, structure:{}, overall:{}}
  zoning_analysis    jsonb not null default '{}'::jsonb,            -- ADU feasibility, setbacks, caveats
  lead_rationale     jsonb not null default '{}'::jsonb,            -- property-only factors behind outreach

  -- Compliance firewall log: every finding, redaction, and disclaimer applied
  compliance_log     jsonb not null default '[]'::jsonb,
  compliance_passed  boolean not null default false,

  -- Pipeline errors (e.g. vision call failed). Non-empty => status is compliance_hold.
  errors             jsonb not null default '[]'::jsonb,

  report_markdown    text,
  report_html        text,
  model_versions     jsonb not null default '{}'::jsonb,            -- {vision: 'gpt-...', firewall: 'rules-v1', ...}

  generated_at       timestamptz not null default now(),
  approved_by        uuid references auth.users(id) on delete set null,
  approved_at        timestamptz,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),

  unique (parcel_id, version)
);

create index if not exists dossiers_parcel_idx on dossiers (parcel_id);
create index if not exists dossiers_status_idx on dossiers (status);

drop trigger if exists dossiers_set_updated_at on dossiers;
create trigger dossiers_set_updated_at before update on dossiers
  for each row execute function set_updated_at();

-- =============================================================================
-- dossiers.version — assigned in the database so concurrent pipeline runs for
-- the same parcel cannot both compute max+1 and collide on unique(parcel_id, version).
-- =============================================================================
create or replace function dossiers_assign_version() returns trigger language plpgsql as $$
begin
  -- Serialize per parcel; the advisory lock is released at transaction end.
  perform pg_advisory_xact_lock(hashtext(new.parcel_id::text));
  select coalesce(max(version), 0) + 1 into new.version from dossiers where parcel_id = new.parcel_id;
  return new;
end $$;

drop trigger if exists dossiers_assign_version on dossiers;
create trigger dossiers_assign_version before insert on dossiers
  for each row execute function dossiers_assign_version();

-- =============================================================================
-- RPC: parcels_in_bbox — lightweight map payload for the mobile client.
--
-- security definer: the function reads property_scans across all agents (so the
-- map can show "already scanned" rings) while the table's own RLS keeps agents
-- from reading each other's scan rows directly. It exposes only the scan status.
-- No equity-based cap: the client decides what to draw at a given zoom.
-- =============================================================================
create index if not exists property_scans_parcel_captured_idx on property_scans (parcel_id, captured_at desc);

create or replace function parcels_in_bbox(
  min_lon double precision,
  min_lat double precision,
  max_lon double precision,
  max_lat double precision,
  max_rows integer default 5000
)
returns table (
  id                  uuid,
  apn                 text,
  situs_address       text,
  lon                 double precision,
  lat                 double precision,
  estimated_equity    numeric,
  zoning_code         text,
  lot_area_sqft       numeric,
  latest_scan_status  scan_status
)
language sql stable
security definer
set search_path = public, pg_temp
as $$
  select
    p.id,
    p.apn,
    p.situs_address,
    st_x(p.centroid) as lon,
    st_y(p.centroid) as lat,
    p.estimated_equity,
    p.zoning_code,
    p.lot_area_sqft,
    s.status as latest_scan_status
  from parcels p
  left join lateral (
    select ps.status from property_scans ps
    where ps.parcel_id = p.id
    order by ps.captured_at desc
    limit 1
  ) s on true
  where p.centroid && st_makeenvelope(min_lon, min_lat, max_lon, max_lat, 4326)
  order by p.apn
  limit least(greatest(max_rows, 1), 5000);
$$;

revoke all on function parcels_in_bbox(double precision, double precision, double precision, double precision, integer) from public, anon;
grant execute on function parcels_in_bbox(double precision, double precision, double precision, double precision, integer) to authenticated, service_role;

-- =============================================================================
-- Privileges & Row Level Security
--
-- Threat model: the mobile app ships the anon key. Anyone who extracts it can
-- obtain an `authenticated` JWT if anonymous sign-ins are enabled, so
-- "authenticated" must never mean "trusted". Rules:
--   * anon gets nothing.
--   * authenticated agents see parcel PUBLIC-RECORD columns only; owner name,
--     mailing address, and the raw assessor blob are reachable only via the
--     service role (backend).
--   * agents read/write only their own scans and their own storage objects.
--   * dossiers are readable by agents; writes are backend-only.
-- Supabase grants ALL on new public tables to anon/authenticated by default, so
-- the revokes below are load-bearing.
-- =============================================================================
revoke all on parcels, property_scans, dossiers from anon, authenticated;

-- Column-level grant: everything except owner PII and the raw assessor blob.
grant select (
  id, apn, situs_address, situs_city, situs_zip, jurisdiction, zoning_code, zoning_desc,
  land_use_code, lot_area_sqft, year_built, living_area_sqft, owner_occupied,
  assessed_land_value, assessed_improvement_value, assessed_total_value, tax_year,
  last_sale_date, last_sale_price, estimated_market_value, estimated_equity,
  equity_confidence, equity_model_version, centroid, source, source_ingested_at,
  created_at, updated_at
) on parcels to authenticated;

grant select, insert, update on property_scans to authenticated;
grant select on dossiers to authenticated;

alter table parcels         enable row level security;
alter table property_scans  enable row level security;
alter table dossiers        enable row level security;

drop policy if exists "parcels readable by authenticated" on parcels;
create policy "parcels readable by authenticated" on parcels
  for select to authenticated using (true);

drop policy if exists "scans readable by authenticated" on property_scans;
drop policy if exists "scans readable by owner" on property_scans;
create policy "scans readable by owner" on property_scans
  for select to authenticated using (captured_by = (select auth.uid()));

drop policy if exists "scans insertable by owner" on property_scans;
create policy "scans insertable by owner" on property_scans
  for insert to authenticated with check (captured_by = (select auth.uid()));

drop policy if exists "scans updatable by owner" on property_scans;
create policy "scans updatable by owner" on property_scans
  for update to authenticated
  using (captured_by = (select auth.uid()))
  with check (captured_by = (select auth.uid()));

drop policy if exists "dossiers readable by authenticated" on dossiers;
create policy "dossiers readable by authenticated" on dossiers
  for select to authenticated using (true);

-- =============================================================================
-- Storage: private bucket for exterior photos / LiDAR captures.
-- Objects are scoped to their uploader. The UPDATE policy is required for
-- idempotent retries (`upsert: true` on an object that already exists).
-- =============================================================================
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('property-scans', 'property-scans', false, 26214400,
        array['image/jpeg', 'image/png', 'image/heic', 'application/octet-stream', 'model/obj', 'model/gltf-binary'])
on conflict (id) do nothing;

drop policy if exists "scan objects: authenticated upload" on storage.objects;
drop policy if exists "scan objects: authenticated read" on storage.objects;
drop policy if exists "scan objects: owner insert" on storage.objects;
drop policy if exists "scan objects: owner select" on storage.objects;
drop policy if exists "scan objects: owner update" on storage.objects;

create policy "scan objects: owner insert" on storage.objects
  for insert to authenticated
  with check (bucket_id = 'property-scans' and owner_id = (select auth.uid())::text);

create policy "scan objects: owner select" on storage.objects
  for select to authenticated
  using (bucket_id = 'property-scans' and owner_id = (select auth.uid())::text);

create policy "scan objects: owner update" on storage.objects
  for update to authenticated
  using (bucket_id = 'property-scans' and owner_id = (select auth.uid())::text)
  with check (bucket_id = 'property-scans' and owner_id = (select auth.uid())::text);
