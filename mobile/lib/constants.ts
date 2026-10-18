import type { Region } from 'react-native-maps';

/** Harmony Grove / Harmony Grove Village, SW of downtown Escondido (ZIP 92029). */
export const HARMONY_GROVE_REGION: Region = {
  latitude: 33.0935,
  longitude: -117.1175,
  latitudeDelta: 0.035,
  longitudeDelta: 0.035,
};

export const STUDY_AREA_ZIP = '92029';

/** Supabase Storage bucket for exterior photos + LiDAR captures (private). */
export const SCAN_BUCKET = 'property-scans';

export const MAX_PHOTOS_PER_SCAN = 6;
export const MAX_MARKERS = 500;

/** Equity tiers drive marker color only — never contact priority on their own. */
export const EQUITY_TIERS = [
  { min: 750_000, color: '#0b7285', label: 'High' },
  { min: 350_000, color: '#2b8a3e', label: 'Mid' },
  { min: 0, color: '#868e96', label: 'Low' },
] as const;

export function equityColor(equity: number | null): string {
  if (equity == null) return '#adb5bd';
  return (EQUITY_TIERS.find((t) => equity >= t.min) ?? EQUITY_TIERS[EQUITY_TIERS.length - 1]).color;
}

export function formatMoney(v: number | null | undefined): string {
  if (v == null) return '—';
  return v.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
}

export function formatSqft(v: number | null | undefined): string {
  if (v == null) return '—';
  return `${Math.round(v).toLocaleString('en-US')} sq ft`;
}

/**
 * Columns the `authenticated` role may read from `parcels`. The schema grants
 * column-level SELECT only — owner name, mailing address and the raw assessor
 * blob are backend-only — so `select('*')` is rejected by PostgREST. Keep this
 * in sync with the GRANT in backend/ingestion/schema.sql.
 */
export const PARCEL_PUBLIC_COLUMNS =
  'id,apn,situs_address,situs_city,situs_zip,jurisdiction,zoning_code,zoning_desc,' +
  'land_use_code,lot_area_sqft,year_built,living_area_sqft,owner_occupied,' +
  'assessed_land_value,assessed_improvement_value,assessed_total_value,tax_year,' +
  'last_sale_date,last_sale_price,estimated_market_value,estimated_equity,' +
  'equity_confidence,equity_model_version,source,source_ingested_at,created_at,updated_at';
