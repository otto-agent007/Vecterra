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
 * MVP auth: anonymous sign-in so RLS policies (`to authenticated`) apply.
 * Enable "Anonymous sign-ins" in Supabase Auth settings. Replace with real
 * field-agent accounts before any multi-user rollout.
 */
export async function ensureSession(): Promise<string | null> {
  const { data } = await supabase.auth.getSession();
  if (data.session) return data.session.user.id;
  const { data: anon, error } = await supabase.auth.signInAnonymously();
  if (error) {
    console.warn('[vecterra] anonymous sign-in failed:', error.message);
    return null;
  }
  return anon.user?.id ?? null;
}
