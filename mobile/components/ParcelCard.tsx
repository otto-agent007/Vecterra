import { Pressable, StyleSheet, Text, View } from 'react-native';

import { formatMoney, formatSqft } from '@/lib/constants';
import type { Parcel } from '@/lib/types';

interface Props {
  parcel: Parcel;
  onClose: () => void;
  onCapture: () => void;
  onDetail: () => void;
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <View style={styles.row}>
      <Text style={styles.label}>{label}</Text>
      <Text style={styles.value}>{value}</Text>
    </View>
  );
}

/** Bottom sheet shown when a map marker is tapped. */
export default function ParcelCard({ parcel, onClose, onCapture, onDetail }: Props) {
  return (
    <View style={styles.card}>
      <View style={styles.header}>
        <View style={{ flex: 1 }}>
          <Text style={styles.address} numberOfLines={1}>
            {parcel.situs_address ?? 'Address unavailable'}
          </Text>
          <Text style={styles.apn}>APN {parcel.apn}</Text>
        </View>
        <Pressable onPress={onClose} hitSlop={12} accessibilityLabel="Close">
          <Text style={styles.close}>✕</Text>
        </Pressable>
      </View>

      <Row label="Zoning" value={parcel.zoning_code ?? '—'} />
      <Row label="Lot" value={formatSqft(parcel.lot_area_sqft)} />
      <Row label="Built" value={parcel.year_built ? String(parcel.year_built) : '—'} />
      <Row label="Est. equity (model)" value={formatMoney(parcel.estimated_equity)} />

      <View style={styles.actions}>
        <Pressable style={[styles.button, styles.secondary]} onPress={onDetail}>
          <Text style={styles.secondaryText}>Details</Text>
        </Pressable>
        <Pressable style={[styles.button, styles.primary]} onPress={onCapture}>
          <Text style={styles.primaryText}>Capture exterior</Text>
        </Pressable>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    position: 'absolute',
    left: 12,
    right: 12,
    bottom: 16,
    backgroundColor: '#fff',
    borderRadius: 16,
    padding: 16,
    shadowColor: '#000',
    shadowOpacity: 0.18,
    shadowRadius: 10,
    shadowOffset: { width: 0, height: 4 },
    elevation: 6,
  },
  header: { flexDirection: 'row', alignItems: 'flex-start', marginBottom: 10 },
  address: { fontSize: 16, fontWeight: '700', color: '#1f2933' },
  apn: { fontSize: 12, color: '#52606d', marginTop: 2 },
  close: { fontSize: 18, color: '#52606d', paddingLeft: 12 },
  row: { flexDirection: 'row', justifyContent: 'space-between', paddingVertical: 4 },
  label: { color: '#52606d', fontSize: 13 },
  value: { color: '#1f2933', fontSize: 13, fontWeight: '600' },
  actions: { flexDirection: 'row', gap: 10, marginTop: 12 },
  button: { flex: 1, paddingVertical: 12, borderRadius: 10, alignItems: 'center' },
  primary: { backgroundColor: '#0b7285' },
  primaryText: { color: '#fff', fontWeight: '700' },
  secondary: { backgroundColor: '#e3f2f5' },
  secondaryText: { color: '#0b7285', fontWeight: '700' },
});
