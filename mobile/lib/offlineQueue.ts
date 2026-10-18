/**
 * Offline-first upload queue.
 *
 * Captures are written to the device first (photos in the app's document
 * directory, metadata in AsyncStorage). `flushQueue` uploads them to Supabase
 * Storage + `property_scans` whenever the device is online. The root layout
 * calls `flushQueue` on every connectivity change, so field agents can shoot
 * all day in a dead zone and sync when they drive back into coverage.
 */

import AsyncStorage from '@react-native-async-storage/async-storage';
import NetInfo from '@react-native-community/netinfo';
import { decode } from 'base64-arraybuffer';
import * as FileSystem from 'expo-file-system/legacy';

import { SCAN_BUCKET } from './constants';
import { ensureSession, supabase } from './supabase';
import type { ParcelSummary, QueuedScan } from './types';

const QUEUE_KEY = 'vecterra.scanQueue.v1';
const PARCEL_CACHE_KEY = 'vecterra.parcelCache.v1';
const MAX_ATTEMPTS = 5;

// ---------------------------------------------------------------------------
// Queue persistence
// ---------------------------------------------------------------------------
export async function listQueue(): Promise<QueuedScan[]> {
  try {
    const raw = await AsyncStorage.getItem(QUEUE_KEY);
    return raw ? (JSON.parse(raw) as QueuedScan[]) : [];
  } catch {
    return [];
  }
}

async function writeQueue(queue: QueuedScan[]): Promise<void> {
  await AsyncStorage.setItem(QUEUE_KEY, JSON.stringify(queue));
}

export async function enqueueScan(scan: QueuedScan): Promise<void> {
  const queue = await listQueue();
  const idx = queue.findIndex((q) => q.scanId === scan.scanId);
  if (idx >= 0) queue[idx] = scan;
  else queue.push(scan);
  await writeQueue(queue);
}

export async function removeFromQueue(scanId: string, deleteFiles = true): Promise<void> {
  const queue = await listQueue();
  const target = queue.find((q) => q.scanId === scanId);
  await writeQueue(queue.filter((q) => q.scanId !== scanId));
  if (deleteFiles && target) {
    const dir = scanDirectory(target.scanId);
    await FileSystem.deleteAsync(dir, { idempotent: true }).catch(() => {});
  }
}

export function scanDirectory(scanId: string): string {
  return `${FileSystem.documentDirectory ?? ''}scans/${scanId}/`;
}

// ---------------------------------------------------------------------------
// Upload
// ---------------------------------------------------------------------------
async function isOnline(): Promise<boolean> {
  const state = await NetInfo.fetch();
  return Boolean(state.isConnected) && state.isInternetReachable !== false;
}

async function uploadFile(localUri: string, objectPath: string, contentType: string): Promise<void> {
  const b64 = await FileSystem.readAsStringAsync(localUri, { encoding: FileSystem.EncodingType.Base64 });
  const { error } = await supabase.storage.from(SCAN_BUCKET).upload(objectPath, decode(b64), {
    contentType,
    upsert: true,
  });
  if (error) throw new Error(`storage upload failed for ${objectPath}: ${error.message}`);
}

async function uploadScan(scan: QueuedScan): Promise<void> {
  const userId = await ensureSession();

  const imagePaths: string[] = [];
  for (let i = 0; i < scan.photos.length; i++) {
    const objectPath = `${scan.apn}/${scan.scanId}/photo-${i + 1}.jpg`;
    await uploadFile(scan.photos[i].uri, objectPath, 'image/jpeg');
    imagePaths.push(objectPath);
  }

  let lidarPath: string | null = null;
  if (scan.lidarUri) {
    lidarPath = `${scan.apn}/${scan.scanId}/mesh.obj`;
    await uploadFile(scan.lidarUri, lidarPath, 'model/obj');
  }

  const row = {
    id: scan.scanId,
    apn: scan.apn,
    parcel_id: scan.parcelId,
    captured_by: userId,
    captured_at: scan.capturedAt,
    uploaded_at: new Date().toISOString(),
    status: 'uploaded' as const,
    capture_location: scan.location
      ? `SRID=4326;POINT(${scan.location.longitude} ${scan.location.latitude})`
      : null,
    heading_deg: scan.headingDeg,
    image_paths: imagePaths,
    lidar_path: lidarPath,
    edge_cv_metadata: {
      ...scan.edgeCvMetadata,
      photos: scan.photos.map((p, i) => ({
        index: i + 1,
        width: p.width,
        height: p.height,
        captured_at: p.capturedAt,
        heading_deg: p.headingDeg,
      })),
    },
    device_metadata: scan.deviceMetadata,
  };

  const { error } = await supabase.from('property_scans').upsert(row, { onConflict: 'id' });
  if (error) throw new Error(`property_scans upsert failed: ${error.message}`);
}

let flushing = false;

export interface FlushResult {
  uploaded: number;
  failed: number;
  skipped: boolean;
}

/** Upload every queued scan. Safe to call repeatedly; concurrent calls coalesce. */
export async function flushQueue(onProgress?: (done: number, total: number) => void): Promise<FlushResult> {
  if (flushing) return { uploaded: 0, failed: 0, skipped: true };
  if (!(await isOnline())) return { uploaded: 0, failed: 0, skipped: true };

  flushing = true;
  let uploaded = 0;
  let failed = 0;
  try {
    const queue = await listQueue();
    for (let i = 0; i < queue.length; i++) {
      const scan = queue[i];
      if (scan.attempts >= MAX_ATTEMPTS) {
        failed++;
        continue;
      }
      try {
        await uploadScan(scan);
        await removeFromQueue(scan.scanId);
        uploaded++;
      } catch (err) {
        failed++;
        await enqueueScan({
          ...scan,
          attempts: scan.attempts + 1,
          lastError: err instanceof Error ? err.message : String(err),
        });
      }
      onProgress?.(i + 1, queue.length);
    }
  } finally {
    flushing = false;
  }
  return { uploaded, failed, skipped: false };
}

// ---------------------------------------------------------------------------
// Parcel cache (lets the map render the last known markers while offline)
// ---------------------------------------------------------------------------
export async function getCachedParcels(): Promise<ParcelSummary[]> {
  try {
    const raw = await AsyncStorage.getItem(PARCEL_CACHE_KEY);
    return raw ? (JSON.parse(raw) as ParcelSummary[]) : [];
  } catch {
    return [];
  }
}

export async function setCachedParcels(parcels: ParcelSummary[]): Promise<void> {
  await AsyncStorage.setItem(PARCEL_CACHE_KEY, JSON.stringify(parcels));
}
