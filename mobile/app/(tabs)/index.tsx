import { useRouter } from 'expo-router';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ActivityIndicator, StyleSheet, Text, View } from 'react-native';
import MapView, { type Region } from 'react-native-maps';

import ParcelCard from '@/components/ParcelCard';
import ParcelMarker from '@/components/ParcelMarker';
import { HARMONY_GROVE_BBOX, HARMONY_GROVE_REGION, MARKER_MAX_LAT_DELTA, MAX_MARKERS, PARCEL_PUBLIC_COLUMNS } from '@/lib/constants';
import { getCachedParcelRow, getCachedParcels, setCachedParcelRow, setCachedParcels } from '@/lib/offlineQueue';
import { supabase } from '@/lib/supabase';
import { summaryToParcel, type Parcel, type ParcelSummary } from '@/lib/types';

/**
 * Map screen.
 *
 * Offline-first strategy: the whole study area (a few thousand summaries,
 * ~300 KB) is fetched once per session and cached on-device, then filtered
 * client-side as the agent pans. There is no per-pan network round trip, so
 * the map is identical with or without signal. The cache is refreshed in the
 * background whenever a fetch succeeds.
 */
export default function MapScreen() {
  const router = useRouter();
  const [all, setAll] = useState<ParcelSummary[]>([]);
  const [region, setRegion] = useState<Region>(HARMONY_GROVE_REGION);
  const [selected, setSelected] = useState<Parcel | null>(null);
  const [loading, setLoading] = useState(true);
  const [source, setSource] = useState<'live' | 'cache' | 'none'>('none');
  const [cachedAt, setCachedAt] = useState<string | null>(null);

  // Monotonic sequence numbers so an older response can never overwrite a newer one.
  const parcelFetchSeq = useRef(0);
  const detailFetchSeq = useRef(0);

  const loadStudyArea = useCallback(async () => {
    const seq = ++parcelFetchSeq.current;
    setLoading(true);

    // 1. Paint from cache immediately.
    const cached = await getCachedParcels();
    if (seq === parcelFetchSeq.current && cached.parcels.length) {
      setAll(cached.parcels);
      setSource('cache');
      setCachedAt(cached.cachedAt);
    }

    // 2. Refresh from the server for the whole bbox.
    const [minLon, minLat, maxLon, maxLat] = HARMONY_GROVE_BBOX;
    const { data, error } = await supabase.rpc('parcels_in_bbox', {
      min_lon: minLon,
      min_lat: minLat,
      max_lon: maxLon,
      max_lat: maxLat,
      max_rows: MAX_MARKERS,
    });
    if (seq !== parcelFetchSeq.current) return; // superseded
    if (!error && data) {
      const rows = data as ParcelSummary[];
      setAll(rows);
      setSource('live');
      setCachedAt(new Date().toISOString());
      setCachedParcels(rows).catch(() => {});
    } else if (!cached.parcels.length) {
      setSource('none');
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    loadStudyArea();
  }, [loadStudyArea]);

  // Client-side viewport filter. Marker rendering is gated by zoom: above the
  // threshold react-native-maps would be drawing thousands of custom views.
  const zoomedIn = region.latitudeDelta <= MARKER_MAX_LAT_DELTA;
  const visible = useMemo(() => {
    if (!zoomedIn) return [];
    const minLat = region.latitude - region.latitudeDelta / 2;
    const maxLat = region.latitude + region.latitudeDelta / 2;
    const minLon = region.longitude - region.longitudeDelta / 2;
    const maxLon = region.longitude + region.longitudeDelta / 2;
    return all.filter((p) => p.lat >= minLat && p.lat <= maxLat && p.lon >= minLon && p.lon <= maxLon);
  }, [all, region, zoomedIn]);

  /** Tapping a marker shows the summary at once, then the full row (cache first, then live). */
  const onMarkerPress = async (summary: ParcelSummary) => {
    const seq = ++detailFetchSeq.current;
    setSelected(summaryToParcel(summary));

    const cachedRow = await getCachedParcelRow(summary.apn);
    if (seq !== detailFetchSeq.current) return;
    if (cachedRow) setSelected({ ...cachedRow, lon: summary.lon, lat: summary.lat });

    const { data, error } = await supabase
      .from('parcels')
      .select<string, Parcel>(PARCEL_PUBLIC_COLUMNS)
      .eq('apn', summary.apn)
      .single();
    if (seq !== detailFetchSeq.current) return;
    if (!error && data) {
      setSelected({ ...data, lon: summary.lon, lat: summary.lat });
      setCachedParcelRow(data).catch(() => {});
    }
  };

  const bannerText = !zoomedIn
    ? `Zoom in to see parcels · ${all.length} loaded`
    : `${visible.length} of ${all.length} parcels${source === 'cache' ? ' · offline (cached)' : source === 'none' ? ' · no data yet' : ''}`;

  return (
    <View style={styles.container}>
      <MapView
        style={StyleSheet.absoluteFill}
        initialRegion={HARMONY_GROVE_REGION}
        onRegionChangeComplete={setRegion}
        onPress={() => setSelected(null)}
        showsUserLocation
        showsCompass
      >
        {visible.map((p) => (
          <ParcelMarker key={p.id} parcel={p} selected={selected?.apn === p.apn} onPress={() => onMarkerPress(p)} />
        ))}
      </MapView>

      <View style={styles.banner} pointerEvents="none">
        <Text style={styles.bannerText}>{bannerText}</Text>
        {loading && <ActivityIndicator size="small" color="#0b7285" style={{ marginLeft: 8 }} />}
      </View>
      {source === 'cache' && cachedAt && (
        <View style={[styles.banner, styles.subBanner]} pointerEvents="none">
          <Text style={styles.subBannerText}>Parcel data from {new Date(cachedAt).toLocaleString()}</Text>
        </View>
      )}

      {selected && (
        <ParcelCard
          parcel={selected}
          onClose={() => setSelected(null)}
          onCapture={() => router.push({ pathname: '/capture/[apn]', params: { apn: selected.apn, parcelId: selected.id } })}
          onDetail={() => router.push({ pathname: '/parcel/[apn]', params: { apn: selected.apn } })}
        />
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
  banner: {
    position: 'absolute',
    top: 56,
    alignSelf: 'center',
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: 'rgba(255,255,255,0.92)',
    paddingHorizontal: 14,
    paddingVertical: 8,
    borderRadius: 20,
    shadowColor: '#000',
    shadowOpacity: 0.15,
    shadowRadius: 6,
    elevation: 3,
  },
  bannerText: { fontSize: 13, fontWeight: '600', color: '#1f2933' },
  subBanner: { top: 96, paddingVertical: 4, backgroundColor: 'rgba(255,243,205,0.95)' },
  subBannerText: { fontSize: 11, color: '#7a5a00' },
});
