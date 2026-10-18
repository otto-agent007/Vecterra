-- RLS / privilege invariants for the Vecterra schema.
-- Runs after ci_prelude.sql + ingestion/schema.sql. Any failed assertion raises
-- and makes psql exit non-zero (run with ON_ERROR_STOP=1).
--
-- Each block switches to the `authenticated` role with a fake JWT sub, so these
-- are the exact permission checks an app user would hit through PostgREST.

\set ON_ERROR_STOP on
\set agent_a '11111111-1111-1111-1111-111111111111'
\set agent_b '22222222-2222-2222-2222-222222222222'

begin;

-- Fixture ----------------------------------------------------------------------
insert into auth.users (id) values (:'agent_a'), (:'agent_b');

insert into parcels (apn, situs_address, situs_zip, owner_name, owner_mailing_address, tax_data, estimated_equity, geom)
values ('TEST-001', '1 Test Rd', '92029', 'SECRET OWNER', 'PO BOX 1', '{"secret":1}', 500000,
        st_geomfromtext('POLYGON((-117.12 33.09,-117.11 33.09,-117.11 33.10,-117.12 33.10,-117.12 33.09))', 4326));

insert into property_scans (id, apn, captured_by, image_paths)
select '33333333-3333-3333-3333-333333333333', 'TEST-001', :'agent_a', '{TEST-001/x/photo-1.jpg}';

insert into storage.objects (bucket_id, name, owner_id)
values ('property-scans', 'TEST-001/x/photo-1.jpg', :'agent_a');

-- Helper: assert a statement raises insufficient_privilege ---------------------
create or replace function pg_temp.assert_denied(stmt text, label text) returns void language plpgsql as $$
begin
  begin
    execute stmt;
  exception
    when insufficient_privilege then return;
  end;
  raise exception 'ASSERTION FAILED (%): expected insufficient_privilege, statement succeeded', label;
end $$;

create or replace function pg_temp.assert_count(stmt text, expected bigint, label text) returns void language plpgsql as $$
declare n bigint;
begin
  execute stmt into n;
  if n <> expected then
    raise exception 'ASSERTION FAILED (%): expected % rows, got %', label, expected, n;
  end if;
end $$;

-- 1. anon has no access at all ------------------------------------------------
set local role anon;
select pg_temp.assert_denied('select id from parcels limit 1',           'anon cannot read parcels');
select pg_temp.assert_denied('select id from property_scans limit 1',    'anon cannot read scans');
select pg_temp.assert_denied('select id from dossiers limit 1',          'anon cannot read dossiers');
select pg_temp.assert_denied('select * from parcels_in_bbox(-180,-90,180,90)', 'anon cannot call parcels_in_bbox');
reset role;

-- 2. authenticated agent A -------------------------------------------------------
set local role authenticated;
select set_config('request.jwt.claim.sub', :'agent_a', true);

-- public-record columns are readable
select pg_temp.assert_count('select count(*) from parcels where apn = ''TEST-001''', 1, 'agent reads public parcel columns');
select pg_temp.assert_count('select count(*) from (select apn, situs_address, estimated_equity, zoning_code from parcels) t', 1, 'agent reads allowed column list');

-- owner PII and raw assessor blob are NOT
select pg_temp.assert_denied('select owner_name from parcels',            'agent cannot read owner_name');
select pg_temp.assert_denied('select owner_mailing_address from parcels', 'agent cannot read owner_mailing_address');
select pg_temp.assert_denied('select tax_data from parcels',              'agent cannot read tax_data');
select pg_temp.assert_denied('select * from parcels',                     'select * on parcels is denied (forces explicit columns)');

-- agents cannot write parcels or dossiers
select pg_temp.assert_denied('update parcels set situs_address = ''x''',  'agent cannot update parcels');
select pg_temp.assert_denied('insert into dossiers (parcel_id, health_scores) select id, ''{}'' from parcels', 'agent cannot insert dossiers');

-- own scan is visible; map RPC works and sees the scan status
select pg_temp.assert_count('select count(*) from property_scans', 1, 'agent A sees own scan');
select pg_temp.assert_count('select count(*) from parcels_in_bbox(-118, 33, -117, 34) where latest_scan_status is not null', 1, 'RPC exposes scan status');

-- own storage object is readable and updatable (idempotent retry)
select pg_temp.assert_count('select count(*) from storage.objects', 1, 'agent A sees own object');
update storage.objects set created_at = now() where name = 'TEST-001/x/photo-1.jpg';

-- cannot forge captured_by
select pg_temp.assert_denied(
  format('insert into property_scans (apn, captured_by) values (''TEST-001'', %L)', :'agent_b'),
  'agent A cannot insert a scan as agent B');
-- (RLS with-check violations surface as insufficient_privilege: 42501)

-- 3. authenticated agent B sees nothing of A's ----------------------------------------
select set_config('request.jwt.claim.sub', :'agent_b', true);
select pg_temp.assert_count('select count(*) from property_scans', 0, 'agent B cannot see agent A scan');
select pg_temp.assert_count('select count(*) from storage.objects', 0, 'agent B cannot see agent A object');
select pg_temp.assert_count('select count(*) from parcels_in_bbox(-118, 33, -117, 34) where latest_scan_status is not null', 1, 'RPC still shows scanned ring to agent B');
-- update of A's object is silently a no-op under RLS (0 rows), never a leak
select pg_temp.assert_count('with u as (update storage.objects set created_at = now() where name = ''TEST-001/x/photo-1.jpg'' returning 1) select count(*) from u', 0, 'agent B cannot update agent A object');

reset role;

-- 4. dossier version assignment is DB-side and monotonic --------------------------------
insert into dossiers (parcel_id, health_scores, version) select id, '{}', 99 from parcels where apn = 'TEST-001';
insert into dossiers (parcel_id, health_scores)             select id, '{}'     from parcels where apn = 'TEST-001';
select pg_temp.assert_count('select count(*) from dossiers d join parcels p on p.id = d.parcel_id where p.apn = ''TEST-001'' and d.version in (1,2)', 2, 'trigger assigns versions 1,2 ignoring client-supplied value');

rollback;
\echo RLS assertions passed
