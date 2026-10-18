import { useRouter } from 'expo-router';
import { useState } from 'react';
import { ActivityIndicator, KeyboardAvoidingView, Platform, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';

import { ALLOW_ANON_AUTH, supabase } from '@/lib/supabase';

/**
 * Field-agent sign-in (email + password accounts created in Supabase Auth).
 * The root layout routes here whenever there is no session. In dev builds
 * with EXPO_PUBLIC_ALLOW_ANON_AUTH=true a "continue anonymously" shortcut is
 * offered; it never appears in release builds.
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

  return (
    <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined} style={styles.container}>
      <View style={styles.card}>
        <Text style={styles.title}>Vecterra</Text>
        <Text style={styles.sub}>Field agent sign-in</Text>

        <TextInput
          style={styles.input}
          placeholder="Email"
          autoCapitalize="none"
          autoComplete="email"
          keyboardType="email-address"
          textContentType="username"
          value={email}
          onChangeText={setEmail}
        />
        <TextInput
          style={styles.input}
          placeholder="Password"
          secureTextEntry
          autoComplete="password"
          textContentType="password"
          value={password}
          onChangeText={setPassword}
          onSubmitEditing={signIn}
        />
        {error && <Text style={styles.error}>{error}</Text>}

        <Pressable style={[styles.button, (busy || !email || !password) && styles.disabled]} onPress={signIn} disabled={busy || !email || !password}>
          {busy ? <ActivityIndicator color="#fff" /> : <Text style={styles.buttonText}>Sign in</Text>}
        </Pressable>

        {ALLOW_ANON_AUTH && (
          <Pressable onPress={signInAnon} disabled={busy} style={{ marginTop: 16 }}>
            <Text style={styles.link}>Continue anonymously (dev build)</Text>
          </Pressable>
        )}
      </View>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: '#f8fafc', justifyContent: 'center', padding: 24 },
  card: { backgroundColor: '#fff', borderRadius: 16, padding: 24, gap: 12, shadowColor: '#000', shadowOpacity: 0.08, shadowRadius: 12, elevation: 3 },
  title: { fontSize: 26, fontWeight: '800', color: '#0b7285' },
  sub: { color: '#52606d', marginBottom: 8 },
  input: { borderWidth: 1, borderColor: '#d9e2ec', borderRadius: 10, paddingHorizontal: 12, paddingVertical: 12, fontSize: 16, color: '#1f2933' },
  error: { color: '#c92a2a', fontSize: 13 },
  button: { backgroundColor: '#0b7285', paddingVertical: 14, borderRadius: 10, alignItems: 'center', marginTop: 4 },
  buttonText: { color: '#fff', fontWeight: '700', fontSize: 16 },
  disabled: { opacity: 0.4 },
  link: { color: '#0b7285', textAlign: 'center', fontWeight: '600' },
});
