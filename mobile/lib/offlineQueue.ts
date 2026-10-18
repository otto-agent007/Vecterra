/**
 * Offline-first upload queue.
 *
 * Captures are written to the device first (photos in the app's document
 * directory, metadata in AsyncStorage). `flushQueue` uploads them to Supabase
 * Storage + `property_scans` whenever the device is online. The root layout
 * calls `flushQueue` on every connectivity change, so field agents can shoot
 * all day in a dead zone and sync when they drive back into coverage.
 *
 * Invariants:
 *  - Every read-modify-write of the queue runs under one mutex, so a flush
 *    finishing while a capture is being saved can neither lose the new scan
 *    nor resurrect the finished one.
 *  - Each photo tracks its own `uploaded` flag; a retry skips objects that
 *    already landed, so a scan that half-uploaded before signal dropped
 *    completes on the next pass instead of re-sending everything.
 *  - Network-level failures (offline, timeout) never consume an attempt.
 *    Only server rejections do, with exponential backoff between them.
 */

import AsyncStorage from '@react-native-async-storage/async-storage';
import NetInfo from '@react-native-community/netinfo';
import { decode } from 'base64-arraybuffer';
import * as FileSystem from 'expo-file-system/legacy';

import { SCAN_BUCKET } from './constants';
import { ensureSession, supabase } from './supabase';
import type { Parcel, ParcelSummary, QueuedScan } from './types';

const QUEUE_KEY = 'vecterra.scanQueue.v2';
const PARCEL_CACHE_KEY = 'vecterra.parcelCache.v2';
const PARCEL_ROW_CACHE_PREFIX = 'vecterra.parcelRow.v1.';

export const MAX_ATTEMPTS = 8;
const BACKOFF_BASE_MS = 30_000; // 30s, 1m, 2m, 4m ... capped
const BACKOFF_MAX_MS = 30 * 60_000;

// ---------------------------------------------------------------------------
// Mutex — serializes every queue mutation. Cheap promise chain; no deps.
// ---------------------------------------------------------------------------
let chain: Promise<unknown> = Promise.resolve();

function withQueueLock<T>(fn: () => Promise<T>): Promise<T> {
  const run = chain.then(fn, fn);
  chain = run.catch(() => undefined);
  return run;
}

// ---------------------------------------------------------------------------
// Queue persistence (callers outside this module get read-only snapshots)
// ---------------------------------------------------------------------------
async function readQueue(): Promise<QueuedScan[]> {
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

export function listQueue(): Promise<QueuedScan[]> {
  return withQueueLock(readQueue);
}

export function enqueueScan(scan: QueuedScan): Promise<void> {
  return withQueueLock(async () => {
    const queue = await readQueue();
    const idx = queue.findIndex((q) => q.scanId === scan.scanId);
    if (idx >= 0) queue[idx] = scan;
    else queue.push(scan);
    await writeQueue(queue);
  });
}

/** Patch one scan in place (used by the uploader to persist per-photo progress). */
function patchScan(scanId: string, patch: (s: QueuedScan) => QueuedScan): Promise<QueuedScan | null> {
  return withQueueLock(async () => {
    const queue = await readQueue();
    const idx = queue.findIndex((q) => q.scanId === scanId);
    if (idx < 0) return null;
    queue[idx] = patch(queue[idx]);
    await writeQueue(queue);
    return queue[idx];
  });
}

export function removeFromQueue(scanId: string, deleteFiles = true): Promise<void> {
  return withQueueLock(async () => {
    const queue = await readQueue();
    const target = queue.find((q) => q.scanId === scanId);
    await writeQueue(queue.filter((q) => q.scanId !== scanId));
    if (deleteFiles && target) {
      await FileSystem.deleteAsync(scanDirectory(target.scanId), { idempotent: true }).catch(() => {});
    }
  });
}

/** Reset a stuck scan so the next flush tries it again immediately. */
export function retryScan(scanId: string): Promise<QueuedScan | null> {
  return patchScan(scanId, (s) => ({ ...s, attempts: 0, nextAttemptAt: null, lastError: null }));
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

/** Errors that mean "try again later" rather than "the server said no". */
class TransientError extends Error {}

function isNetworkError(err: unknown): boolean {
  const msg = err instanceof Error ? err.message : String(err);
  return /network request failed|fetch failed|timeout|timed out|ECONNRESET|ENOTFOUND|Failed to fetch|Load failed/i.test(msg);
}

async function uploadFile(localUri: string, objectPath: string, contentType: string): Promise<void> {
  const info = await FileSystem.getInfoAsync(localUri);
  if (!info.exists) throw new Error(`local file missing: ${localUri}`);
  const b64 = await FileSystem.readAsStringAsync(localUri, { encoding: FileSystem.EncodingType.Base64 });
  const { error } = await supabase.storage.from(SCAN_BUCKET).upload(objectPath, decode(b64), {
    contentType,
    upsert: true, // idempotent retry; requires the storage UPDATE policy in schema.sql
  });
  if (error) {
    if (isNetworkError(error)) throw new TransientError(error.message);
    throw new Error(`storage upload failed for ${objectPath}: ${error.message}`);
  }
}

async function uploadScan(scan: QueuedScan): Promise<void> {
  const userId = await ensureSession();
  if (!userId) throw new TransientError('not signed in');

  // Photos: skip the ones that already landed on a previous attempt.
  const imagePaths: string[] = [];
  for (let i = 0; i < scan.photos.length; i++) {
    const photo = scan.photos[i];
    const objectPath = photo.objectPath ?? `${scan.apn}/${scan.scanId}/${photo.id}.jpg`;
    if (!photo.uploaded) {
      await uploadFile(photo.uri, objectPath, 'image/jpeg');
      const updated = await patchScan(scan.scanId, (s) => ({
        ...s,
        photos: s.photos.map((p) => (p.id === photo.id ? { ...p, uploaded: true, objectPath } : p)),
      }));
      if (updated) scan = updated;
    }
    imagePaths.push(objectPath);
  }

  let lidarPath: string | null = null;
  if (scan.lidarUri) {
    lidarPath = `${scan.apn}/${scan.scanId}/mesh.obj`;
    if (!scan.lidarUploaded) {
      await uploadFile(scan.lidarUri, lidarPath, 'model/obj');
      await patchScan(scan.scanId, (s) => ({ ...s, lidarUploaded: true }));
    }
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
        id: p.id,
        width: p.width,
        height: p.height,
        captured_at: p.capturedAt,
        heading_deg: p.headingDeg,
      })),
    },
    device_metadata: scan.deviceMetadata,
  };

  const { error } = await supabase.from('property_scans').upsert(row, { onConflict: 'id' });
  if (error) {
    if (isNetworkError(error)) throw new TransientError(error.message);
    throw new Error(`property_scans upsert failed: ${error.message}`);
  }
}

function backoffMs(attempts: number): number {
  return Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * 2 ** Math.max(0, attempts - 1));
}

let flushing = false;

export type FlushResult =
  | { kind: 'done'; uploaded: number; failed: number; deferred: number }
  | { kind: 'offline' }
  | { kind: 'busy' }
  | { kind: 'not_signed_in' };

/**
 * Upload every eligible queued scan. Safe to call repeatedly; a concurrent
 * call returns `busy` (not "offline") so the UI can say the right thing.
 */
export async function flushQueue(onProgress?: (done: number, total: number) => void): Promise<FlushResult> {
  if (flushing) return { kind: 'busy' };
  if (!(await isOnline())) return { kind: 'offline' };
  if (!(await ensureSession())) return { kind: 'not_signed_in' };

  flushing = true;
  let uploaded = 0;
  let failed = 0;
  let deferred = 0;
  try {
    const queue = await listQueue();
    const now = Date.now();
    for (let i = 0; i < queue.length; i++) {
      const scan = queue[i];
      if (scan.attempts >= MAX_ATTEMPTS) {
        failed++;
        continue;
      }
      if (scan.nextAttemptAt && new Date(scan.nextAttemptAt).getTime() > now) {
        deferred++;
        continue;
      }
      try {
        await uploadScan(scan);
        await removeFromQueue(scan.scanId);
        uploaded++;
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        if (err instanceof TransientError || isNetworkError(err)) {
          // Don't burn an attempt on a dropped connection; just note it.
          await patchScan(scan.scanId, (s) => ({ ...s, lastError: message }));
          deferred++;
          if (!(await isOnline())) break; // signal is gone; stop hammering
        } else {
          const attempts = scan.attempts + 1;
          await patchScan(scan.scanId, (s) => ({
            ...s,
            attempts,
            lastError: message,
            nextAttemptAt: new Date(Date.now() + backoffMs(attempts)).toISOString(),
          }));
          failed++;
        }
      }
      onProgress?.(i + 1, queue.length);
    }
  } finally {
    flushing = false;
  }
  return { kind: 'done', uploaded, failed, deferred };
}

// ---------------------------------------------------------------------------
// Parcel caches — the whole study area is small enough to keep on-device.
// ---------------------------------------------------------------------------
export async function getCachedParcels(): Promise<{ parcels: ParcelSummary[]; cachedAt: string | null }> {
  try {
    const raw = await AsyncStorage.getItem(PARCEL_CACHE_KEY);
    if (!raw) return { parcels: [], cachedAt: null };
    const parsed = JSON.parse(raw) as { parcels: ParcelSummary[]; cachedAt: string };
    return parsed;
  } catch {
    return { parcels: [], cachedAt: null };
  }
}

export async function setCachedParcels(parcels: ParcelSummary[]): Promise<void> {
  await AsyncStorage.setItem(PARCEL_CACHE_KEY, JSON.stringify({ parcels, cachedAt: new Date().toISOString() }));
}

export async function getCachedParcelRow(apn: string): Promise<Parcel | null> {
  try {
    const raw = await AsyncStorage.getItem(PARCEL_ROW_CACHE_PREFIX + apn);
    return raw ? (JSON.parse(raw) as Parcel) : null;
  } catch {
    return null;
  }
}

export async function setCachedParcelRow(parcel: Parcel): Promise<void> {
  await AsyncStorage.setItem(PARCEL_ROW_CACHE_PREFIX + parcel.apn, JSON.stringify(parcel));
}
