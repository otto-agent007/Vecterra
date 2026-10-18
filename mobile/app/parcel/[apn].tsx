import { useLocalSearchParams, useRouter } from 'expo-router';
import { useEffect, useState } from 'react';
import { ActivityIndicator, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';

import { PARCEL_PUBLIC_COLUMNS, formatMoney, formatSqft } from '@/lib/constants';
import { getCachedParcelRow, setCachedParcelRow } from '@/lib/offlineQueue';
import { supabase } from '@/lib/supabase';
import type { Parcel, ScanStatus } from '@/lib/types';

interface ScanRow {
  id: string;
  captured_at: string;
  status: ScanStatus;
  image_paths: string[];
}

interface DossierRow {
  id: string;
  version: number;
  status: string;
  compliance_passed: boolean;
  health_scores: Record<string, { score: number | null; condition: string } | null>;
  generated_at: string;
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <View style={styles.section}>
      <Text style={styles.sectionTitle}>{title}</Text>
      {children}
    </View>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <View style={styles.row}>
      <Text style={styles.label}>{label}</Text>
      <Text style={styles.value}>{value}</Text>
    </View>
  );
}

/** Parcel detail: public-record attributes, scan history, and the latest dossier status. */
export default function ParcelDetailScreen() {
  const { apn } = useLocalSearchParams<{ apn: string }>();
  const router = useRouter();
  const [parcel, setParcel] = useState<Parcel | null>(null);
  const [scans, setScans] = useState<ScanRow[]>([]);
  const [dossier, setDossier] = useState<DossierRow | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      // Cache first so the screen works in a dead zone; refresh live if we can.
      const cached = await getCachedParcelRow(String(apn));
      if (cached) setParcel(cached);

      const { data: p, error: pe } = await supabase.from('parcels').select<string, Parcel>(PARCEL_PUBLIC_COLUMNS).eq('apn', apn).single();
      if (pe || !p) {
        if (!cached) setError(pe ? 'Could not load parcel — you may be offline.' : 'Parcel not found');
        return;
      }
      setParcel(p as Parcel);
      setCachedParcelRow(p as Parcel).catch(() => {});
      const [{ data: s }, { data: d }] = await Promise.all([
        supabase.from('property_scans').select('id, captured_at, status, image_paths').eq('parcel_id', p.id).order('captured_at', { ascending: false }),
        supabase.from('dossiers').select('id, version, status, compliance_passed, health_scores, generated_at').eq('parcel_id', p.id).order('version', { ascending: false }).limit(1),
      ]);
      setScans((s ?? []) as ScanRow[]);
      setDossier(((d ?? [])[0] as DossierRow | undefined) ?? null);
    })().catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, [apn]);

  if (error) return <View style={styles.center}><Text style={styles.error}>{error}</Text></View>;
  if (!parcel) return <View style={styles.center}><ActivityIndicator /></View>;

  return (
    <ScrollView contentContainerStyle={styles.content}>
      <Text style={styles.address}>{parcel.situs_address ?? 'Address unavailable'}</Text>
      <Text style={styles.sub}>APN {parcel.apn} · {parcel.situs_city ?? ''} {parcel.situs_zip ?? ''}</Text>

      <Section title="Lot & zoning">
        <Row label="Jurisdiction" value={parcel.jurisdiction.replace(/_/g, ' ')} />
        <Row label="Zoning" value={parcel.zoning_code ?? '—'} />
        <Row label="Lot area" value={formatSqft(parcel.lot_area_sqft)} />
        <Row label="Living area" value={formatSqft(parcel.living_area_sqft)} />
        <Row label="Year built" value={parcel.year_built ? String(parcel.year_built) : '—'} />
      </Section>

      <Section title="Public record (county assessor)">
        <Row label={`Assessed total${parcel.tax_year ? ` (${parcel.tax_year})` : ''}`} value={formatMoney(parcel.assessed_total_value)} />
        <Row label="Last sale" value={parcel.last_sale_date ? `${parcel.last_sale_date} · ${formatMoney(parcel.last_sale_price)}` : '—'} />
        <Row label="Owner-occupied" value={parcel.owner_occupied == null ? '—' : parcel.owner_occupied ? 'Yes' : 'No'} />
      </Section>

      <Section title="Model estimates (not an appraisal)">
        <Row label="Est. market value" value={formatMoney(parcel.estimated_market_value)} />
        <Row label="Est. equity" value={formatMoney(parcel.estimated_equity)} />
        <Row label="Confidence" value={parcel.equity_confidence != null ? `${Math.round(parcel.equity_confidence * 100)}%` : '—'} />
      </Section>

      <Section title={`Field scans (${scans.length})`}>
        {scans.length === 0 && <Text style={styles.muted}>No exterior captures yet.</Text>}
        {scans.map((s) => (
          <Row key={s.id} label={new Date(s.captured_at).toLocaleString()} value={`${s.status} · ${s.image_paths.length} photo(s)`} />
        ))}
      </Section>

      <Section title="Latest dossier">
        {!dossier && <Text style={styles.muted}>No dossier generated yet.</Text>}
        {dossier && (
          <>
            <Row label="Version / status" value={`v${dossier.version} · ${dossier.status}`} />
            <Row label="Compliance firewall" value={dossier.compliance_passed ? 'Passed' : 'Hold — review required'} />
            {(['roof', 'paint', 'structure', 'overall'] as const).map((k) => (
              <Row key={k} label={k} value={dossier.health_scores?.[k]?.score != null ? `${dossier.health_scores[k]!.score}/100 · ${dossier.health_scores[k]!.condition}` : '—'} />
            ))}
          </>
        )}
      </Section>

      <Pressable
        style={styles.primary}
        onPress={() => router.push({ pathname: '/capture/[apn]', params: { apn: parcel.apn, parcelId: parcel.id } })}
      >
        <Text style={styles.primaryText}>Capture exterior</Text>
      </Pressable>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  content: { padding: 16, gap: 12 },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center' },
  error: { color: '#c92a2a' },
  address: { fontSize: 20, fontWeight: '700', color: '#1f2933' },
  sub: { color: '#52606d', marginBottom: 4 },
  section: { backgroundColor: '#fff', borderRadius: 12, padding: 14, borderWidth: StyleSheet.hairlineWidth, borderColor: '#d9e2ec' },
  sectionTitle: { fontWeight: '700', color: '#0b7285', marginBottom: 6 },
  row: { flexDirection: 'row', justifyContent: 'space-between', paddingVertical: 4, gap: 12 },
  label: { color: '#52606d', fontSize: 13, flexShrink: 1 },
  value: { color: '#1f2933', fontSize: 13, fontWeight: '600', textAlign: 'right', flexShrink: 1 },
  muted: { color: '#868e96', fontSize: 13 },
  primary: { backgroundColor: '#0b7285', paddingVertical: 14, borderRadius: 12, alignItems: 'center', marginTop: 4 },
  primaryText: { color: '#fff', fontWeight: '700' },
});
