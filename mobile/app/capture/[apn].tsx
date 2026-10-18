import { CameraView, useCameraPermissions } from 'expo-camera';
import Constants from 'expo-constants';
import * as Crypto from 'expo-crypto';
import * as FileSystem from 'expo-file-system/legacy';
import * as Location from 'expo-location';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { useEffect, useRef, useState } from 'react';
import { ActivityIndicator, Alert, Image, Platform, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { MAX_PHOTOS_PER_SCAN } from '@/lib/constants';
import { captureLidarMesh, lidarAvailable } from '@/lib/lidar';
import { enqueueScan, flushQueue, scanDirectory } from '@/lib/offlineQueue';
import type { CaptureLocation, CapturedPhoto, QueuedScan } from '@/lib/types';

/**
 * Exterior capture screen.
 *
 * Flow: shoot up to N photos (from the public right-of-way) → optional LiDAR
 * mesh → everything is written to the device and queued → upload is attempted
 * immediately and retried automatically by the root layout when back online.
 */
export default function CaptureScreen() {
  const { apn, parcelId } = useLocalSearchParams<{ apn: string; parcelId?: string }>();
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const cameraRef = useRef<CameraView>(null);

  const [cameraPermission, requestCameraPermission] = useCameraPermissions();
  const [scanId] = useState(() => Crypto.randomUUID());
  const [photos, setPhotos] = useState<CapturedPhoto[]>([]);
  const [lidarUri, setLidarUri] = useState<string | null>(null);
  const [lidarSupported, setLidarSupported] = useState(false);
  const [location, setLocation] = useState<CaptureLocation | null>(null);
  const [busy, setBusy] = useState<'photo' | 'lidar' | 'save' | null>(null);
  const [cameraReady, setCameraReady] = useState(false);
  const savedRef = useRef(false);

  // Orphan cleanup: if the agent backs out without saving, nothing stays on disk.
  useEffect(() => {
    return () => {
      if (!savedRef.current) {
        FileSystem.deleteAsync(scanDirectory(scanId), { idempotent: true }).catch(() => {});
      }
    };
  }, [scanId]);

  useEffect(() => {
    if (!cameraPermission?.granted) requestCameraPermission();
    lidarAvailable().then(setLidarSupported);
    (async () => {
      const { status } = await Location.requestForegroundPermissionsAsync();
      if (status !== 'granted') return;
      const pos = await Location.getCurrentPositionAsync({ accuracy: Location.Accuracy.High });
      setLocation({
        latitude: pos.coords.latitude,
        longitude: pos.coords.longitude,
        accuracyM: pos.coords.accuracy ?? null,
      });
    })().catch(() => {});
  }, [cameraPermission?.granted, requestCameraPermission]);

  const readHeading = async (): Promise<number | null> => {
    try {
      const h = await Location.getHeadingAsync();
      return h.trueHeading >= 0 ? h.trueHeading : h.magHeading;
    } catch {
      return null;
    }
  };

  const takePhoto = async () => {
    if (!cameraRef.current || !cameraReady || busy || photos.length >= MAX_PHOTOS_PER_SCAN) return;
    setBusy('photo');
    try {
      const shot = await cameraRef.current.takePictureAsync({ quality: 0.85, exif: true, skipProcessing: false });
      if (!shot) return;
      const dir = scanDirectory(scanId);
      await FileSystem.makeDirectoryAsync(dir, { intermediates: true });
      // UUID basename: deleting photo 1 of 3 and shooting again can never
      // collide with an existing file (moveAsync onto an existing path fails on iOS).
      const id = Crypto.randomUUID();
      const dest = `${dir}${id}.jpg`;
      await FileSystem.moveAsync({ from: shot.uri, to: dest });
      const headingDeg = await readHeading();
      setPhotos((prev) => [
        ...prev,
        { id, uri: dest, width: shot.width, height: shot.height, capturedAt: new Date().toISOString(), headingDeg },
      ]);
    } catch (err) {
      Alert.alert('Capture failed', err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  const takeLidar = async () => {
    if (busy) return;
    if (!lidarSupported) {
      Alert.alert(
        'LiDAR unavailable',
        'LiDAR capture needs an iPhone/iPad Pro and the native lidar-capture module (see mobile/lib/lidar.ts). Photos still upload normally.',
      );
      return;
    }
    setBusy('lidar');
    try {
      const dir = scanDirectory(scanId);
      await FileSystem.makeDirectoryAsync(dir, { intermediates: true });
      const mesh = await captureLidarMesh(dir);
      if (mesh) setLidarUri(mesh.uri);
    } catch (err) {
      Alert.alert('LiDAR failed', err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  const removePhoto = async (index: number) => {
    const target = photos[index];
    await FileSystem.deleteAsync(target.uri, { idempotent: true }).catch(() => {});
    setPhotos((prev) => prev.filter((_, i) => i !== index));
  };

  const save = async () => {
    if (photos.length === 0 || busy) return;
    setBusy('save');
    try {
      const scan: QueuedScan = {
        scanId,
        apn: String(apn),
        parcelId: parcelId ? String(parcelId) : null,
        capturedAt: photos[0].capturedAt,
        photos,
        lidarUri,
        location,
        headingDeg: photos[0].headingDeg,
        // Edge CV placeholder: populate from an on-device model (blur/exposure/facade bbox)
        // before upload. Keeping raw fields here means the backend can re-score later.
        edgeCvMetadata: {
          pipeline: 'edge-cv-none-v0',
          photo_count: photos.length,
          lidar: lidarUri != null,
        },
        deviceMetadata: {
          platform: Platform.OS,
          os_version: String(Platform.Version),
          app_version: Constants.expoConfig?.version ?? 'dev',
        },
        attempts: 0,
        nextAttemptAt: null,
        lastError: null,
      };

      // 1) Persist locally first — this is the offline-first guarantee.
      await enqueueScan(scan);
      savedRef.current = true;
      // 2) Opportunistic upload; failures stay queued and retry on reconnect.
      const result = await flushQueue();
      const msg =
        result.kind === 'offline'
          ? 'Saved to the upload queue — it will sync when you are back online.'
          : result.kind === 'not_signed_in'
            ? 'Saved to the upload queue — sign in to sync.'
            : result.kind === 'busy'
              ? 'Saved; a sync is already running and will pick it up.'
              : result.uploaded > 0 && result.failed === 0 && result.deferred === 0
                ? 'Uploaded.'
                : 'Saved; upload will retry automatically.';
      Alert.alert('Capture saved', msg, [{ text: 'OK', onPress: () => router.back() }]);
    } catch (err) {
      Alert.alert('Save failed', err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  if (!cameraPermission) {
    return <View style={styles.center}><ActivityIndicator /></View>;
  }
  if (!cameraPermission.granted) {
    return (
      <View style={styles.center}>
        <Text style={styles.permissionText}>Camera access is required to capture exterior photos.</Text>
        <Pressable style={styles.primaryButton} onPress={requestCameraPermission}>
          <Text style={styles.primaryButtonText}>Grant camera access</Text>
        </Pressable>
        <Pressable onPress={() => router.back()} style={{ marginTop: 16 }}>
          <Text style={styles.link}>Cancel</Text>
        </Pressable>
      </View>
    );
  }

  return (
    <View style={styles.container}>
      <CameraView ref={cameraRef} style={StyleSheet.absoluteFill} facing="back" onCameraReady={() => setCameraReady(true)} />

      {/* Top bar */}
      <View style={[styles.topBar, { paddingTop: insets.top + 8 }]}>
        <Pressable onPress={() => router.back()} hitSlop={12}>
          <Text style={styles.topText}>✕ Cancel</Text>
        </Pressable>
        <Text style={styles.topText}>APN {apn}</Text>
        <Text style={styles.topText}>{photos.length}/{MAX_PHOTOS_PER_SCAN}</Text>
      </View>

      {/* Guidance */}
      <View style={styles.guide} pointerEvents="none">
        <Text style={styles.guideText}>Shoot from the public right-of-way: full front elevation, roofline, each visible side.</Text>
        {location && (
          <Text style={styles.guideSub}>
            GPS ±{location.accuracyM ? Math.round(location.accuracyM) : '?'} m{lidarUri ? ' · LiDAR mesh captured' : ''}
          </Text>
        )}
      </View>

      {/* Thumbnails */}
      {photos.length > 0 && (
        <ScrollView horizontal style={styles.strip} contentContainerStyle={{ gap: 8, paddingHorizontal: 12 }}>
          {photos.map((p, i) => (
            <Pressable key={p.id} onLongPress={() => removePhoto(i)}>
              <Image source={{ uri: p.uri }} style={styles.thumb} />
            </Pressable>
          ))}
        </ScrollView>
      )}

      {/* Controls */}
      <View style={[styles.controls, { paddingBottom: insets.bottom + 16 }]}>
        <Pressable style={styles.sideButton} onPress={takeLidar} disabled={busy != null}>
          <Text style={styles.sideButtonText}>{busy === 'lidar' ? '…' : 'LiDAR'}</Text>
        </Pressable>

        <Pressable
          style={[styles.shutter, (busy != null || photos.length >= MAX_PHOTOS_PER_SCAN) && styles.disabled]}
          onPress={takePhoto}
          disabled={busy != null || photos.length >= MAX_PHOTOS_PER_SCAN}
          accessibilityLabel="Take photo"
        >
          {busy === 'photo' ? <ActivityIndicator color="#fff" /> : <View style={styles.shutterInner} />}
        </Pressable>

        <Pressable
          style={[styles.sideButton, styles.save, photos.length === 0 && styles.disabled]}
          onPress={save}
          disabled={photos.length === 0 || busy != null}
        >
          <Text style={styles.sideButtonText}>{busy === 'save' ? 'Saving…' : 'Save'}</Text>
        </Pressable>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: '#000' },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center', padding: 24, backgroundColor: '#fff' },
  permissionText: { fontSize: 16, textAlign: 'center', color: '#1f2933', marginBottom: 16 },
  primaryButton: { backgroundColor: '#0b7285', paddingHorizontal: 20, paddingVertical: 12, borderRadius: 10 },
  primaryButtonText: { color: '#fff', fontWeight: '700' },
  link: { color: '#0b7285', fontWeight: '600' },
  topBar: {
    position: 'absolute', top: 0, left: 0, right: 0,
    flexDirection: 'row', justifyContent: 'space-between', paddingHorizontal: 16, paddingBottom: 10,
    backgroundColor: 'rgba(0,0,0,0.45)',
  },
  topText: { color: '#fff', fontWeight: '700', fontSize: 14 },
  guide: { position: 'absolute', top: 110, left: 16, right: 16, backgroundColor: 'rgba(0,0,0,0.45)', padding: 10, borderRadius: 10 },
  guideText: { color: '#fff', fontSize: 13 },
  guideSub: { color: '#dee2e6', fontSize: 12, marginTop: 4 },
  strip: { position: 'absolute', bottom: 130, left: 0, right: 0, maxHeight: 72 },
  thumb: { width: 64, height: 64, borderRadius: 8, borderWidth: 2, borderColor: '#fff' },
  controls: {
    position: 'absolute', bottom: 0, left: 0, right: 0,
    flexDirection: 'row', alignItems: 'center', justifyContent: 'space-around',
    paddingTop: 16, backgroundColor: 'rgba(0,0,0,0.45)',
  },
  shutter: { width: 76, height: 76, borderRadius: 38, borderWidth: 4, borderColor: '#fff', alignItems: 'center', justifyContent: 'center' },
  shutterInner: { width: 58, height: 58, borderRadius: 29, backgroundColor: '#fff' },
  sideButton: { minWidth: 84, paddingVertical: 12, paddingHorizontal: 14, borderRadius: 10, backgroundColor: 'rgba(255,255,255,0.2)', alignItems: 'center' },
  save: { backgroundColor: '#0b7285' },
  sideButtonText: { color: '#fff', fontWeight: '700' },
  disabled: { opacity: 0.4 },
});
