import 'react-native-url-polyfill/auto';
import AsyncStorage from '@react-native-async-storage/async-storage';
import { createClient } from '@supabase/supabase-js';

const url = process.env.EXPO_PUBLIC_SUPABASE_URL;
const anonKey = process.env.EXPO_PUBLIC_SUPABASE_ANON_KEY;

if (!url || !anonKey) {
  // Fail loudly in dev; a silent empty client produces confusing "fetch failed" errors later.
  console.warn('[vecterra] EXPO_PUBLIC_SUPABASE_URL / EXPO_PUBLIC_SUPABASE_ANON_KEY are not set (see mobile/.env.example)');
}

export const supabase = createClient(url ?? 'http://localhost', anonKey ?? 'anon', {
  auth: {
    storage: AsyncStorage,
    autoRefreshToken: true,
    persistSession: true,
    detectSessionInUrl: false,
  },
});

/**
 * Returns the signed-in user id, or null when there is no session.
 *
 * Anonymous sign-in is a DEV-ONLY convenience: with it enabled, anyone who
 * extracts the anon key from the app binary becomes an `authenticated` user,
 * and every RLS policy written "to authenticated" applies to them. It is gated
 * behind EXPO_PUBLIC_ALLOW_ANON_AUTH=true so a production build cannot ship it
 * by accident. Production builds must use real agent accounts (see the sign-in
 * flow) and keep "Anonymous sign-ins" OFF in the Supabase Auth settings.
 */
export const ALLOW_ANON_AUTH = process.env.EXPO_PUBLIC_ALLOW_ANON_AUTH === 'true' && __DEV__;

export async function ensureSession(): Promise<string | null> {
  const { data } = await supabase.auth.getSession();
  if (data.session) return data.session.user.id;
  if (!ALLOW_ANON_AUTH) return null;
  const { data: anon, error } = await supabase.auth.signInAnonymously();
  if (error) {
    console.warn('[vecterra] anonymous sign-in failed:', error.message);
    return null;
  }
  return anon.user?.id ?? null;
}
