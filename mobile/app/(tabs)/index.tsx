import { useRouter } from 'expo-router';
import { useCallback, useEffect, useRef, useState } from 'react';
import { ActivityIndicator, StyleSheet, Text, View } from 'react-native';
import MapView, { type Region } from 'react-native-maps';

import ParcelCard from '@/components/ParcelCard';
import ParcelMarker from '@/components/ParcelMarker';
import { HARMONY_GROVE_REGION, MAX_MARKERS } from '@/lib/constants';
import { getCachedParcels, setCachedParcels } from '@/lib/offlineQueue';
import { supabase } from '@/lib/supabase';
import { summaryToParcel, type Parcel, type ParcelSummary } from '@/lib/types';

const FETCH_DEBOUNCE_MS = 400;

export default function MapScreen() {
  const router = useRouter();
  const [parcels, setParcels] = useState<ParcelSummary[]>([]);
  const [selected, setSelected] = useState<Parcel | null>(null);
  const [loading, setLoading] = useState(false);
  const [offline, setOffline] = useState(false);
  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null);

  /** Fetch map markers for the visible region via the `parcels_in_bbox` RPC. */
  const loadParcels = useCallback(async (region: Region) => {
    setLoading(true);
    const { data, error } = await supabase.rpc('parcels_in_bbox', {
      min_lon: region.longitude - region.longitudeDelta / 2,
      min_lat: region.latitude - region.latitudeDelta / 2,
      max_lon: region.longitude + region.longitudeDelta / 2,
      max_lat: region.latitude + region.latitudeDelta / 2,
      max_rows: MAX_MARKERS,
    });

    if (error || !data) {
      // Offline (or RPC error): fall back to the last successful fetch.
      const cached = await getCachedParcels();
      setParcels(cached);
      setOffline(true);
    } else {
      const rows = data as ParcelSummary[];
      setParcels(rows);
      setOffline(false);
      setCachedParcels(rows).catch(() => {});
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    loadParcels(HARMONY_GROVE_REGION);
    return () => {
      if (debounce.current) clearTimeout(debounce.current);
    };
  }, [loadParcels]);

  const onRegionChangeComplete = (region: Region) => {
    if (debounce.current) clearTimeout(debounce.current);
    debounce.current = setTimeout(() => loadParcels(region), FETCH_DEBOUNCE_MS);
  };

  /** Tapping a marker fetches the full parcel row; degrade to the summary if offline. */
  const onMarkerPress = async (summary: ParcelSummary) => {
    setSelected(summaryToParcel(summary));
    const { data, error } = await supabase.from('parcels').select('*').eq('apn', summary.apn).single();
    if (!error && data) {
      setSelected({ ...(data as Parcel), lon: summary.lon, lat: summary.lat });
    }
  };

  return (
    <View style={styles.container}>
      <MapView
        style={StyleSheet.absoluteFill}
        initialRegion={HARMONY_GROVE_REGION}
        onRegionChangeComplete={onRegionChangeComplete}
        onPress={() => setSelected(null)}
        showsUserLocation
        showsCompass
      >
        {parcels.map((p) => (
          <ParcelMarker key={p.id} parcel={p} selected={selected?.apn === p.apn} onPress={() => onMarkerPress(p)} />
        ))}
      </MapView>

      <View style={styles.banner} pointerEvents="none">
        <Text style={styles.bannerText}>
          Harmony Grove · {parcels.length} parcel{parcels.length === 1 ? '' : 's'}
          {offline ? ' · offline (cached)' : ''}
        </Text>
        {loading && <ActivityIndicator size="small" color="#0b7285" style={{ marginLeft: 8 }} />}
      </View>

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
});
