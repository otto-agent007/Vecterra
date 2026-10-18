import { useRouter } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { useState } from 'react';
import {
  ActivityIndicator,
  Image,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';

import { BRAND } from '@/lib/constants';
import { ALLOW_ANON_AUTH, supabase } from '@/lib/supabase';

const LOGO = require('@/assets/brand/vecterra-logo.png');
const LOGO_ASPECT = 900 / 786; // width / height of assets/brand/vecterra-logo.png

/**
 * Field-agent sign-in (email + password accounts created in Supabase Auth).
 * The root layout routes here whenever there is no session. In dev builds
 * with EXPO_PUBLIC_ALLOW_ANON_AUTH=true a "continue anonymously" shortcut is
 * offered; it never appears in release builds.
 *
 * The screen uses the logo's own canvas color as its background so the
 * glowing mark sits on it seamlessly (same color as the splash screen).
 */
export default function SignInScreen() {
  const router = useRouter();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const signIn = async () => {
    setBusy(true);
    setError(null);
    const { error: err } = await supabase.auth.signInWithPassword({ email: email.trim(), password });
    setBusy(false);
    if (err) {
      setError(err.message);
      return;
    }
    router.replace('/');
  };

  const signInAnon = async () => {
    setBusy(true);
    const { error: err } = await supabase.auth.signInAnonymously();
    setBusy(false);
    if (err) setError(err.message);
    else router.replace('/');
  };

  const disabled = busy || !email || !password;

  return (
    <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined} style={styles.container}>
      <StatusBar style="light" />
      <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
        <Image source={LOGO} style={styles.logo} resizeMode="contain" accessibilityLabel="Vecterra" />
        <Text style={styles.sub}>Field agent sign-in</Text>

        <View style={styles.form}>
          <TextInput
            style={styles.input}
            placeholder="Email"
            placeholderTextColor={BRAND.textMuted}
            autoCapitalize="none"
            autoComplete="email"
            keyboardType="email-address"
            textContentType="username"
            keyboardAppearance="dark"
            value={email}
            onChangeText={setEmail}
          />
          <TextInput
            style={styles.input}
            placeholder="Password"
            placeholderTextColor={BRAND.textMuted}
            secureTextEntry
            autoComplete="password"
            textContentType="password"
            keyboardAppearance="dark"
            value={password}
            onChangeText={setPassword}
            onSubmitEditing={signIn}
          />
          {error && <Text style={styles.error}>{error}</Text>}

          <Pressable
            style={({ pressed }) => [styles.button, disabled && styles.disabled, pressed && !disabled && styles.pressed]}
            onPress={signIn}
            disabled={disabled}
            accessibilityRole="button"
          >
            {busy ? <ActivityIndicator color="#fff" /> : <Text style={styles.buttonText}>Sign in</Text>}
          </Pressable>

          {ALLOW_ANON_AUTH && (
            <Pressable onPress={signInAnon} disabled={busy} style={{ marginTop: 8 }}>
              <Text style={styles.link}>Continue anonymously (dev build)</Text>
            </Pressable>
          )}
        </View>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: BRAND.background },
  content: { flexGrow: 1, justifyContent: 'center', alignItems: 'center', padding: 24 },
  logo: { width: 240, height: 240 / LOGO_ASPECT },
  sub: { color: BRAND.textMuted, fontSize: 15, marginTop: 4, marginBottom: 28, letterSpacing: 0.3 },
  form: { width: '100%', maxWidth: 380, gap: 12 },
  input: {
    backgroundColor: BRAND.surface,
    borderWidth: 1,
    borderColor: BRAND.border,
    borderRadius: 12,
    paddingHorizontal: 14,
    paddingVertical: 14,
    fontSize: 16,
    color: BRAND.text,
  },
  error: { color: BRAND.danger, fontSize: 13 },
  button: { backgroundColor: BRAND.indigo, paddingVertical: 15, borderRadius: 12, alignItems: 'center', marginTop: 4 },
  pressed: { opacity: 0.85 },
  buttonText: { color: '#fff', fontWeight: '700', fontSize: 16 },
  disabled: { opacity: 0.4 },
  link: { color: BRAND.wordmark, textAlign: 'center', fontWeight: '600' },
});
