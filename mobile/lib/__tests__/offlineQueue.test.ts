/**
 * Offline queue invariants. All platform modules are mocked; this exercises
 * the mutex, per-photo upload state, backoff, and transient-vs-rejection logic.
 */

import type { QueuedScan } from '../types';

// --- Mocks -----------------------------------------------------------------------
const mockStore = new Map<string, string>();
jest.mock('@react-native-async-storage/async-storage', () => ({
  __esModule: true,
  default: {
    getItem: jest.fn(async (k: string) => mockStore.get(k) ?? null),
    setItem: jest.fn(async (k: string, v: string) => {
      // Simulate real async storage latency so interleavings are possible.
      await new Promise((r) => setTimeout(r, 1));
      mockStore.set(k, v);
    }),
    removeItem: jest.fn(async (k: string) => void mockStore.delete(k)),
  },
}));

let mockOnline = true;
jest.mock('@react-native-community/netinfo', () => ({
  __esModule: true,
  default: { fetch: jest.fn(async () => ({ isConnected: mockOnline, isInternetReachable: mockOnline })), addEventListener: jest.fn() },
}));

const mockDeleted: string[] = [];
jest.mock('expo-file-system/legacy', () => ({
  documentDirectory: 'file:///doc/',
  EncodingType: { Base64: 'base64' },
  getInfoAsync: jest.fn(async () => ({ exists: true })),
  readAsStringAsync: jest.fn(async () => 'QUJD'),
  deleteAsync: jest.fn(async (p: string) => void mockDeleted.push(p)),
}));

jest.mock('base64-arraybuffer', () => ({ decode: () => new ArrayBuffer(3) }));

const mockStorageUpload = jest.fn();
const mockTableUpsert = jest.fn();
let mockUserId: string | null = 'user-1';
jest.mock('../supabase', () => ({
  ensureSession: jest.fn(async () => mockUserId),
  supabase: {
    storage: { from: () => ({ upload: (...a: unknown[]) => mockStorageUpload(...a) }) },
    from: () => ({ upsert: (...a: unknown[]) => mockTableUpsert(...a) }),
  },
}));

// --- Helpers -----------------------------------------------------------------------
import { MAX_ATTEMPTS, enqueueScan, flushQueue, listQueue, removeFromQueue, retryScan } from '../offlineQueue';

function makeScan(id: string, photoCount = 2): QueuedScan {
  return {
    scanId: id,
    apn: '999-001-01-00',
    parcelId: null,
    capturedAt: '2026-10-18T00:00:00Z',
    photos: Array.from({ length: photoCount }, (_, i) => ({
      id: `p${i + 1}`,
      uri: `file:///doc/scans/${id}/p${i + 1}.jpg`,
      width: 1,
      height: 1,
      capturedAt: '2026-10-18T00:00:00Z',
      headingDeg: null,
    })),
    lidarUri: null,
    location: null,
    headingDeg: null,
    edgeCvMetadata: {},
    deviceMetadata: {},
    attempts: 0,
    nextAttemptAt: null,
    lastError: null,
  };
}

beforeEach(() => {
  mockStore.clear();
  mockDeleted.length = 0;
  mockOnline = true;
  mockUserId = 'user-1';
  mockStorageUpload.mockReset().mockResolvedValue({ error: null });
  mockTableUpsert.mockReset().mockResolvedValue({ error: null });
});

// --- Tests --------------------------------------------------------------------------
test('happy path uploads every photo, upserts the row, removes the scan and its files', async () => {
  await enqueueScan(makeScan('A'));
  const r = await flushQueue();
  expect(r).toEqual({ kind: 'done', uploaded: 1, failed: 0, deferred: 0 });
  expect(mockStorageUpload).toHaveBeenCalledTimes(2);
  expect(mockStorageUpload.mock.calls[0][0]).toBe('999-001-01-00/A/p1.jpg');
  expect(mockTableUpsert).toHaveBeenCalledTimes(1);
  expect(await listQueue()).toEqual([]);
  expect(mockDeleted).toContain('file:///doc/scans/A/');
});

test('enqueue during a flush is neither lost nor does the finished scan get resurrected', async () => {
  await enqueueScan(makeScan('A'));
  // Make the upload slow so enqueueScan('B') lands mid-flush.
  mockStorageUpload.mockImplementation(async () => {
    await new Promise((r) => setTimeout(r, 20));
    return { error: null };
  });
  const flush = flushQueue();
  await new Promise((r) => setTimeout(r, 5));
  await enqueueScan(makeScan('B'));
  await flush;

  const queue = await listQueue();
  expect(queue.map((s) => s.scanId)).toEqual(['B']); // A gone, B kept
  expect(queue[0].attempts).toBe(0);
});

test('half-uploaded scan resumes without re-sending photos that already landed', async () => {
  await enqueueScan(makeScan('A', 3));
  // Photo 1 ok, photo 2 drops the connection.
  mockStorageUpload
    .mockResolvedValueOnce({ error: null })
    .mockResolvedValueOnce({ error: { message: 'Network request failed' } });
  const r1 = await flushQueue();
  expect(r1).toEqual({ kind: 'done', uploaded: 0, failed: 0, deferred: 1 });

  const [scan] = await listQueue();
  expect(scan.attempts).toBe(0); // network drop does not burn an attempt
  expect(scan.photos.map((p) => Boolean(p.uploaded))).toEqual([true, false, false]);

  mockStorageUpload.mockClear().mockResolvedValue({ error: null });
  const r2 = await flushQueue();
  expect(r2).toEqual({ kind: 'done', uploaded: 1, failed: 0, deferred: 0 });
  expect(mockStorageUpload).toHaveBeenCalledTimes(2); // only p2 and p3
  expect(mockStorageUpload.mock.calls.map((c) => c[0])).toEqual(['999-001-01-00/A/p2.jpg', '999-001-01-00/A/p3.jpg']);
});

test('server rejection counts an attempt, sets backoff, and is retried only after it', async () => {
  await enqueueScan(makeScan('A', 1));
  mockTableUpsert.mockResolvedValue({ error: { message: 'new row violates row-level security policy' } });
  const r1 = await flushQueue();
  expect(r1).toEqual({ kind: 'done', uploaded: 0, failed: 1, deferred: 0 });
  let [scan] = await listQueue();
  expect(scan.attempts).toBe(1);
  expect(scan.nextAttemptAt && new Date(scan.nextAttemptAt).getTime()).toBeGreaterThan(Date.now());
  expect(scan.lastError).toMatch(/row-level security/);

  // Still inside the backoff window: deferred, not retried.
  mockTableUpsert.mockClear();
  const r2 = await flushQueue();
  expect(r2).toEqual({ kind: 'done', uploaded: 0, failed: 0, deferred: 1 });
  expect(mockTableUpsert).not.toHaveBeenCalled();

  // Manual retry resets the window.
  await retryScan('A');
  mockTableUpsert.mockResolvedValue({ error: null });
  const r3 = await flushQueue();
  expect(r3).toEqual({ kind: 'done', uploaded: 1, failed: 0, deferred: 0 });
  [scan] = await listQueue();
  expect(scan).toBeUndefined();
});

test('scan is parked after MAX_ATTEMPTS rejections and never uploads again until retried', async () => {
  await enqueueScan({ ...makeScan('A', 1), attempts: MAX_ATTEMPTS });
  const r = await flushQueue();
  expect(r).toEqual({ kind: 'done', uploaded: 0, failed: 1, deferred: 0 });
  expect(mockStorageUpload).not.toHaveBeenCalled();
});

test('offline, busy, and not-signed-in are distinguishable', async () => {
  await enqueueScan(makeScan('A', 1));
  mockOnline = false;
  expect(await flushQueue()).toEqual({ kind: 'offline' });

  mockOnline = true;
  mockUserId = null;
  expect(await flushQueue()).toEqual({ kind: 'not_signed_in' });

  mockUserId = 'user-1';
  mockStorageUpload.mockImplementation(async () => {
    await new Promise((r) => setTimeout(r, 20));
    return { error: null };
  });
  const first = flushQueue();
  await new Promise((r) => setTimeout(r, 2));
  expect(await flushQueue()).toEqual({ kind: 'busy' });
  await first;
});

test('removeFromQueue deletes the scan directory', async () => {
  await enqueueScan(makeScan('Z'));
  await removeFromQueue('Z');
  expect(await listQueue()).toEqual([]);
  expect(mockDeleted).toContain('file:///doc/scans/Z/');
});
