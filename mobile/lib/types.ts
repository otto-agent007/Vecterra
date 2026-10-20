// Shared client-side types. Keep in sync with backend/ingestion/schema.sql.
// Regenerate strict DB types later with: supabase gen types typescript --project-id <id>

export type ScanStatus = 'queued' | 'uploaded' | 'processing' | 'scored' | 'failed';
export type Jurisdiction =
  | 'san_diego_county'
  | 'city_of_escondido'
  | 'city_of_san_marcos'
  | 'city_of_san_diego'
  | 'unknown';

/** Lightweight row returned by the `parcels_in_bbox` RPC for map markers. */
export interface ParcelSummary {
  id: string;
  apn: string;
  situs_address: string | null;
  lon: number;
  lat: number;
  estimated_equity: number | null;
  zoning_code: string | null;
  lot_area_sqft: number | null;
  latest_scan_status: ScanStatus | null;
}

/** Full `parcels` row (geometry columns omitted on the client). */
export interface Parcel {
  id: string;
  apn: string;
  situs_address: string | null;
  situs_city: string | null;
  situs_zip: string | null;
  jurisdiction: Jurisdiction;
  zoning_code: string | null;
  zoning_desc: string | null;
  land_use_code: string | null;
  lot_area_sqft: number | null;
  year_built: number | null;
  living_area_sqft: number | null;
  owner_occupied: boolean | null;
  assessed_land_value: number | null;
  assessed_improvement_value: number | null;
  assessed_total_value: number | null;
  tax_year: number | null;
  last_sale_date: string | null;
  last_sale_price: number | null;
  estimated_market_value: number | null;
  estimated_equity: number | null;
  equity_confidence: number | null;
  /** Centroid is derived client-side from the summary when the full row is unavailable. */
  lon?: number;
  lat?: number;
}

export interface CapturedPhoto {
  id: string;           // uuid — also the storage object basename, so deletes never collide
  uri: string;          // file:// path inside the app's document directory
  width: number;
  height: number;
  capturedAt: string;   // ISO
  headingDeg: number | null;
  uploaded?: boolean;   // set by the uploader; retries skip uploaded photos
  objectPath?: string;  // storage path once uploaded
}

export interface CaptureLocation {
  latitude: number;
  longitude: number;
  accuracyM: number | null;
}

/** A scan waiting in the offline queue (or mid-upload). */
export interface QueuedScan {
  scanId: string;       // uuid generated on device
  apn: string;
  parcelId: string | null;
  capturedAt: string;
  photos: CapturedPhoto[];
  lidarUri: string | null;
  lidarUploaded?: boolean;
  location: CaptureLocation | null;
  headingDeg: number | null;
  edgeCvMetadata: Record<string, unknown>;
  deviceMetadata: Record<string, unknown>;
  attempts: number;         // server rejections only; network drops don't count
  nextAttemptAt: string | null; // ISO; exponential backoff after a rejection
  lastError: string | null;
}

export function summaryToParcel(s: ParcelSummary): Parcel {
  return {
    id: s.id,
    apn: s.apn,
    situs_address: s.situs_address,
    situs_city: null,
    situs_zip: null,
    jurisdiction: 'unknown',
    zoning_code: s.zoning_code,
    zoning_desc: null,
    land_use_code: null,
    lot_area_sqft: s.lot_area_sqft,
    year_built: null,
    living_area_sqft: null,
    owner_occupied: null,
    assessed_land_value: null,
    assessed_improvement_value: null,
    assessed_total_value: null,
    tax_year: null,
    last_sale_date: null,
    last_sale_price: null,
    estimated_market_value: null,
    estimated_equity: s.estimated_equity,
    equity_confidence: null,
    lon: s.lon,
    lat: s.lat,
  };
}
