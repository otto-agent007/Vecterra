import { useFocusEffect } from 'expo-router';
import { useCallback, useState } from 'react';
import { Alert, FlatList, Pressable, StyleSheet, Text, View } from 'react-native';

import { MAX_ATTEMPTS, flushQueue, listQueue, removeFromQueue, retryScan } from '@/lib/offlineQueue';
import { supabase } from '@/lib/supabase';
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
      if (r.kind === 'offline') Alert.alert('Offline', 'No connection — captures will upload automatically when you reconnect.');
      else if (r.kind === 'not_signed_in') Alert.alert('Not signed in', 'Sign in to upload captures.');
      else if (r.kind === 'busy') Alert.alert('Sync running', 'A sync is already in progress.');
      else Alert.alert('Sync complete', `${r.uploaded} uploaded · ${r.failed} rejected · ${r.deferred} waiting.`);
    } finally {
      setSyncing(false);
      setProgress(null);
      refresh();
    }
  };

  const actions = (scan: QueuedScan) =>
    Alert.alert(`APN ${scan.apn}`, `${scan.photos.length} photo(s)${scan.lastError ? `\nLast error: ${scan.lastError}` : ''}`, [
      { text: 'Cancel', style: 'cancel' },
      { text: 'Retry now', onPress: () => retryScan(scan.scanId).then(() => sync()) },
      {
        text: 'Discard',
        style: 'destructive',
        onPress: () =>
          Alert.alert('Discard capture?', 'Photos will be deleted from this device.', [
            { text: 'Keep', style: 'cancel' },
            { text: 'Discard', style: 'destructive', onPress: () => removeFromQueue(scan.scanId).then(refresh) },
          ]),
      },
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
          <Pressable style={styles.item} onPress={() => actions(item)}>
            <Text style={styles.itemTitle}>APN {item.apn}</Text>
            <Text style={styles.itemSub}>
              {new Date(item.capturedAt).toLocaleString()} · {item.photos.filter((p) => p.uploaded).length}/{item.photos.length} photo(s) uploaded
              {item.lidarUri ? ' · LiDAR' : ''}
            </Text>
            {item.attempts >= MAX_ATTEMPTS ? (
              <Text style={styles.itemError}>Stopped after {item.attempts} rejections — tap to retry or discard.</Text>
            ) : item.attempts > 0 ? (
              <Text style={styles.itemError}>
                {item.attempts} rejection{item.attempts === 1 ? '' : 's'} · next try {item.nextAttemptAt ? new Date(item.nextAttemptAt).toLocaleTimeString() : 'soon'}
              </Text>
            ) : item.lastError ? (
              <Text style={styles.itemWarn}>Waiting for connection: {item.lastError}</Text>
            ) : null}
          </Pressable>
        )}
      />
      <Text style={styles.hint}>Tap a capture to retry or discard it.</Text>
      <Pressable
        onPress={() =>
          Alert.alert('Sign out?', queue.length ? `${queue.length} capture(s) are still waiting to upload and will stay on this device.` : undefined, [
            { text: 'Cancel', style: 'cancel' },
            { text: 'Sign out', style: 'destructive', onPress: () => supabase.auth.signOut() },
          ])
        }
        style={styles.signOut}
      >
        <Text style={styles.signOutText}>Sign out</Text>
      </Pressable>
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
  itemWarn: { color: '#e67700', fontSize: 12, marginTop: 4 },
  hint: { textAlign: 'center', color: '#868e96', fontSize: 12, padding: 10 },
  signOut: { alignSelf: 'center', paddingVertical: 10, paddingHorizontal: 16, marginBottom: 12 },
  signOutText: { color: '#c92a2a', fontWeight: '600' },
});
