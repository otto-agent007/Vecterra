import { memo } from 'react';
import { StyleSheet, View } from 'react-native';
import { Marker } from 'react-native-maps';

import { equityColor } from '@/lib/constants';
import type { ParcelSummary } from '@/lib/types';

interface Props {
  parcel: ParcelSummary;
  selected?: boolean;
  onPress: () => void;
}

/**
 * Custom marker: color encodes modeled equity tier, a white ring marks
 * parcels that already have a field scan.
 */
function ParcelMarker({ parcel, selected, onPress }: Props) {
  const color = equityColor(parcel.estimated_equity);
  const scanned = parcel.latest_scan_status != null;

  return (
    <Marker
      coordinate={{ latitude: parcel.lat, longitude: parcel.lon }}
      onPress={(e) => {
        e.stopPropagation();
        onPress();
      }}
      tracksViewChanges={false}
      anchor={{ x: 0.5, y: 0.5 }}
    >
      <View
        style={[
          styles.dot,
          { backgroundColor: color },
          scanned && styles.scanned,
          selected && styles.selected,
        ]}
      />
    </Marker>
  );
}

const styles = StyleSheet.create({
  dot: {
    width: 14,
    height: 14,
    borderRadius: 7,
    borderWidth: 1.5,
    borderColor: 'rgba(0,0,0,0.35)',
  },
  scanned: { borderWidth: 3, borderColor: '#ffffff' },
  selected: { width: 20, height: 20, borderRadius: 10, borderColor: '#fcc419', borderWidth: 3 },
});

export default memo(ParcelMarker);
