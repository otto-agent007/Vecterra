import NetInfo from '@react-native-community/netinfo';
import type { Session } from '@supabase/supabase-js';
import { Stack, useRouter, useSegments } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { useEffect, useState } from 'react';
import { ActivityIndicator, View } from 'react-native';

import { BRAND } from '@/lib/constants';
import { flushQueue } from '@/lib/offlineQueue';
import { ensureSession, supabase } from '@/lib/supabase';

export default function RootLayout() {
  const router = useRouter();
  const segments = useSegments();
  const [session, setSession] = useState<Session | null | undefined>(undefined); // undefined = still checking

  // Auth state. ensureSession() may sign in anonymously in dev builds.
  useEffect(() => {
    let mounted = true;
    ensureSession()
      .then(() => supabase.auth.getSession())
      .then(({ data }) => mounted && setSession(data.session))
      .catch(() => mounted && setSession(null));
    const { data: sub } = supabase.auth.onAuthStateChange((_event, s) => setSession(s));
    return () => {
      mounted = false;
      sub.subscription.unsubscribe();
    };
  }, []);

  // Route guard: no session → sign-in; session on sign-in screen → map.
  useEffect(() => {
    if (session === undefined) return;
    const onSignIn = segments[0] === 'sign-in';
    if (!session && !onSignIn) router.replace('/sign-in');
    if (session && onSignIn) router.replace('/');
  }, [session, segments, router]);

  // Offline-first: whenever connectivity returns (and we're signed in), drain the capture queue.
  useEffect(() => {
    if (!session) return;
    flushQueue().catch(() => {});
    const unsubscribe = NetInfo.addEventListener((state) => {
      if (state.isConnected && state.isInternetReachable !== false) {
        flushQueue().catch(() => {});
      }
    });
    return () => unsubscribe();
  }, [session]);

  if (session === undefined) {
    return (
      // Same color as the splash screen and sign-in, so there is no white flash on launch.
      <View style={{ flex: 1, alignItems: 'center', justifyContent: 'center', backgroundColor: BRAND.background }}>
        <ActivityIndicator color={BRAND.wordmark} />
      </View>
    );
  }

  return (
    <>
      <StatusBar style="auto" />
      <Stack>
        <Stack.Screen name="(tabs)" options={{ headerShown: false }} />
        <Stack.Screen name="sign-in" options={{ headerShown: false }} />
        <Stack.Screen name="parcel/[apn]" options={{ title: 'Parcel detail' }} />
        <Stack.Screen
          name="capture/[apn]"
          options={{ title: 'Capture exterior', presentation: 'fullScreenModal', headerShown: false }}
        />
      </Stack>
    </>
  );
}
