import { useFocusEffect } from 'expo-router';
import { useCallback, useState } from 'react';
import { Alert, FlatList, Pressable, StyleSheet, Text, View } from 'react-native';

import { flushQueue, listQueue, removeFromQueue } from '@/lib/offlineQueue';
import type { QueuedScan } from '@/lib/types';

/** Shows captures waiting to upload; lets the agent force a sync or discard one. */
export default function QueueScreen() {
  const [queue, setQueue] = useState<QueuedScan[]>([]);
  const [syncing, setSyncing] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);

  const refresh = useCallback(() => {
    listQueue().then(setQueue);
  }, []);

  useFocusEffect(refresh);

  const sync = async () => {
    setSyncing(true);
    try {
      const r = await flushQueue((done, total) => setProgress(`${done}/${total}`));
      if (r.skipped) Alert.alert('Offline', 'No connection — captures will upload automatically when you reconnect.');
      else Alert.alert('Sync complete', `${r.uploaded} uploaded, ${r.failed} still pending.`);
    } finally {
      setSyncing(false);
      setProgress(null);
      refresh();
    }
  };

  const discard = (scan: QueuedScan) =>
    Alert.alert('Discard capture?', `APN ${scan.apn} · ${scan.photos.length} photo(s) will be deleted from this device.`, [
      { text: 'Keep', style: 'cancel' },
      { text: 'Discard', style: 'destructive', onPress: () => removeFromQueue(scan.scanId).then(refresh) },
    ]);

  return (
    <View style={styles.container}>
      <View style={styles.header}>
        <Text style={styles.title}>{queue.length} pending capture{queue.length === 1 ? '' : 's'}</Text>
        <Pressable style={[styles.sync, (syncing || queue.length === 0) && styles.disabled]} onPress={sync} disabled={syncing || queue.length === 0}>
          <Text style={styles.syncText}>{syncing ? `Syncing ${progress ?? ''}` : 'Sync now'}</Text>
        </Pressable>
      </View>

      <FlatList
        data={queue}
        keyExtractor={(s) => s.scanId}
        contentContainerStyle={{ padding: 16, gap: 10 }}
        ListEmptyComponent={<Text style={styles.empty}>Everything is uploaded.</Text>}
        renderItem={({ item }) => (
          <Pressable style={styles.item} onLongPress={() => discard(item)}>
            <Text style={styles.itemTitle}>APN {item.apn}</Text>
            <Text style={styles.itemSub}>
              {new Date(item.capturedAt).toLocaleString()} · {item.photos.length} photo(s){item.lidarUri ? ' · LiDAR' : ''}
            </Text>
            {item.attempts > 0 && (
              <Text style={styles.itemError}>
                {item.attempts} failed attempt{item.attempts === 1 ? '' : 's'}: {item.lastError}
              </Text>
            )}
          </Pressable>
        )}
      />
      <Text style={styles.hint}>Long-press a capture to discard it.</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: '#f8fafc' },
  header: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', padding: 16, paddingBottom: 0 },
  title: { fontSize: 16, fontWeight: '700', color: '#1f2933' },
  sync: { backgroundColor: '#0b7285', paddingHorizontal: 14, paddingVertical: 8, borderRadius: 8 },
  syncText: { color: '#fff', fontWeight: '700' },
  disabled: { opacity: 0.4 },
  empty: { color: '#868e96', textAlign: 'center', marginTop: 40 },
  item: { backgroundColor: '#fff', borderRadius: 12, padding: 14, borderWidth: StyleSheet.hairlineWidth, borderColor: '#d9e2ec' },
  itemTitle: { fontWeight: '700', color: '#1f2933' },
  itemSub: { color: '#52606d', fontSize: 13, marginTop: 2 },
  itemError: { color: '#c92a2a', fontSize: 12, marginTop: 4 },
  hint: { textAlign: 'center', color: '#868e96', fontSize: 12, padding: 10 },
});
