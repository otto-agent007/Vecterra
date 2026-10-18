import NetInfo from '@react-native-community/netinfo';
import { Stack } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { useEffect } from 'react';

import { flushQueue } from '@/lib/offlineQueue';
import { ensureSession } from '@/lib/supabase';

export default function RootLayout() {
  useEffect(() => {
    ensureSession().catch(() => {});
    // Offline-first: whenever connectivity returns, drain the capture queue.
    const unsubscribe = NetInfo.addEventListener((state) => {
      if (state.isConnected && state.isInternetReachable !== false) {
        flushQueue().catch(() => {});
      }
    });
    return () => unsubscribe();
  }, []);

  return (
    <>
      <StatusBar style="auto" />
      <Stack>
        <Stack.Screen name="(tabs)" options={{ headerShown: false }} />
        <Stack.Screen name="parcel/[apn]" options={{ title: 'Parcel detail' }} />
        <Stack.Screen
          name="capture/[apn]"
          options={{ title: 'Capture exterior', presentation: 'fullScreenModal', headerShown: false }}
        />
      </Stack>
    </>
  );
}
