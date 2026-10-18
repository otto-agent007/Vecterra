<p align="center">
  <img src="mobile/assets/brand/vecterra-logo.png" alt="Vecterra" width="300">
</p>

# Vecterra — Spatial & Structural Equity Engine (MVP)

[![CI](https://github.com/otto-agent007/Vecterra/actions/workflows/ci.yml/badge.svg)](https://github.com/otto-agent007/Vecterra/actions/workflows/ci.yml)

Offline-first proptech system that surfaces **off-market homeowner leads in Harmony Grove, Escondido, CA (ZIP 92029)** from public parcel records, field-captured exterior imagery, and deterministic zoning rules — **without any MLS data feed**.

```
parcel GIS + assessor roll ──► Supabase/PostGIS ──► mobile map ──► field capture (photos / LiDAR)
                                                                         │
                                        dossier ◄── compliance firewall ◄── LangGraph agents (vision · zoning)
```

## Repository layout

```
Vecterra/
├── backend/
│   ├── ingestion/
│   │   ├── schema.sql             # PostGIS tables, RPC, RLS, storage bucket
│   │   ├── load_parcels.py        # GeoPandas → Supabase bulk upsert
│   │   └── sample_parcels.geojson # synthetic fixture (not real parcels)
│   ├── agents/
│   │   ├── state.py               # LangGraph state TypedDicts
│   │   ├── pipeline.py            # Vision → Zoning → Draft → Compliance graph
│   │   ├── zoning_rules.py        # SD County / Escondido ADU rule tables
│   │   ├── compliance.py          # deterministic FHA / FEHA firewall
│   │   └── dossier_generator.py   # Markdown + HTML report
│   ├── api/main.py                # FastAPI trigger + Supabase webhook
│   ├── tests/test_compliance.py
│   ├── requirements.txt
│   └── .env.example
├── mobile/                        # Expo (React Native + TypeScript + Expo Router)
│   ├── app/
│   │   ├── _layout.tsx            # root stack; drains upload queue on reconnect
│   │   ├── (tabs)/index.tsx       # map centered on Harmony Grove; tap marker → parcel card
│   │   ├── (tabs)/queue.tsx       # offline upload queue
│   │   ├── parcel/[apn].tsx       # parcel detail + scan/dossier history
│   │   └── capture/[apn].tsx      # camera / LiDAR capture → Supabase Storage
│   ├── components/                # ParcelMarker, ParcelCard
│   ├── lib/                       # supabase client, offline queue, constants, lidar stub, types
│   ├── package.json · app.json · tsconfig.json · .env.example
└── README.md
```

## 1. Database (Supabase + PostGIS)

1. Create a Supabase project and enable PostGIS (the script does `create extension postgis`).
2. Apply the schema:
   ```bash
   psql "$DATABASE_URL" -f backend/ingestion/schema.sql
   # or paste into the SQL editor, or `supabase db push` after copying it into supabase/migrations/
   ```
3. Create field-agent accounts (Auth → Users). Keep **Anonymous sign-ins OFF** in production — see *Security model* below.

Tables: `parcels` (APN, situs, assessor values, modeled equity, `geometry(Polygon,4326)`), `property_scans` (device-generated UUID, storage paths, edge-CV JSON, capture point), `dossiers` (health scores, zoning JSON, compliance log, errors, report Markdown/HTML). RPC `parcels_in_bbox` feeds the map. Bucket `property-scans` is private; access via RLS + signed URLs.

### Security model

The mobile app ships the anon key, so anything an `authenticated` user can do, an attacker who extracts that key and obtains any session can do too. The schema is written for that threat model and CI proves it against a real PostGIS (`backend/tests/sql/`):

| Role | parcels | property_scans | dossiers | storage `property-scans` |
|---|---|---|---|---|
| `anon` | nothing | nothing | nothing | nothing |
| `authenticated` (agent) | public-record columns only — **no** `owner_name`, `owner_mailing_address`, `tax_data` | own rows only (`captured_by = auth.uid()`) | read | own objects only (insert/select/update) |
| `service_role` (backend) | all | all | all | all |

`select *` on `parcels` is rejected by design; the client uses `PARCEL_PUBLIC_COLUMNS`. `parcels_in_bbox` is `security definer` so the map can show "already scanned" rings without exposing other agents' scan rows. Anonymous sign-in exists only behind `EXPO_PUBLIC_ALLOW_ANON_AUTH=true` in dev builds.

## 2. Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, OPENAI_API_KEY
```

**Ingest parcels** (SanGIS `PARCELS` layer or any GeoJSON/shapefile):
```bash
python ingestion/load_parcels.py --input ingestion/sample_parcels.geojson --zip 92029 --dry-run
python ingestion/load_parcels.py --input data/PARCELS.shp --zip 92029
```
Column names are auto-detected (`APN`, `SITUS_ADDRESS`, `ASR_LAND`, `ASR_IMPR`, …) and overridable with `--columns '{"apn":"APN_8"}'`. Owner name/mailing columns are skipped unless `--include-owner` is passed. Multipolygons are reduced to their largest ring to satisfy the `Polygon` column.

**Run the agent pipeline:**
```bash
python -m agents.pipeline --demo                    # synthetic parcel, no network
python -m agents.pipeline --parcel-apn 999-001-01-00 --persist --out dossier.html
pytest                                              # firewall corpus, hardening, template tests
DATABASE_URL=postgres://... backend/tests/sql/run.sh # apply schema + RLS assertions on a scratch PostGIS
```

**Serve the API:**
```bash
uvicorn api.main:app --reload --port 8000
curl -X POST localhost:8000/pipeline/run -H 'content-type: application/json' -d '{"scan_id":"<uuid>"}'
```
`POST /webhooks/supabase/scan-uploaded` accepts a Supabase Database Webhook on `property_scans` INSERT so dossiers generate automatically after a field upload. The server **refuses to start** without a real `VECTERRA_API_TOKEN` unless `VECTERRA_ENV=dev`.

### Agent graph

| Node | Type | What it does |
|---|---|---|
| `VisionScorerNode` | LLM (vision) | Scores roof / paint / structure 0–100 from signed photo URLs. System prompt forbids describing people or occupant signals. Model set by `VECTERRA_VISION_MODEL`. |
| `ZoningAnalyzerNode` | deterministic | State ADU floor (4 ft side/rear setbacks, 800 sq ft by-right) + county/city tables → feasibility score and caveats. Every number carries a `verify` note. |
| `DraftDossierNode` | template | Jinja2 Markdown from structured state only — no free-form prose. |
| `ComplianceFirewallNode` | deterministic | Regex rule set for FHA + CA FEHA protected classes, steering phrases, distress targeting, occupant inference. Text is NFKC-normalized and stripped of invisible characters and HTML first. Blocks are redacted (overlapping spans merged), warnings route the dossier to `compliance_hold`, Equal Housing + estimate disclaimers appended. Lead rationale keys are whitelisted to property factors. Vision observations are screened by the same rules *before they enter state*, so no unscreened model text reaches the DB, API, or report. `tests/test_firewall_corpus.py` is the must-block / must-pass corpus — add a case whenever the field finds a miss. |

## 3. Mobile

Expo SDK 58 · React Native 0.88 · Expo Router · TypeScript.

```bash
cd mobile
npm install
cp .env.example .env            # EXPO_PUBLIC_SUPABASE_URL / EXPO_PUBLIC_SUPABASE_ANON_KEY
npx expo start                  # scan the QR code with the Expo Go app (iPhone: Camera app)
```

Everything except LiDAR runs in **Expo Go** (map, camera, location, offline queue). LiDAR needs the native module described in `lib/lidar.ts`, which means a development build: `npx expo run:ios` (Xcode) or an EAS build. Expo Go from the App Store only supports the latest SDK; after an SDK bump, update with `npx expo install expo@latest && npx expo install --fix`.

* **Sign-in** with a field-agent email/password account (Supabase Auth). The root layout guards every other route. Dev builds with `EXPO_PUBLIC_ALLOW_ANON_AUTH=true` get an anonymous shortcut; release builds never do.
* **Map** opens on Harmony Grove (33.0935, −117.1175). The **whole study area** is fetched once via `parcels_in_bbox` and cached on-device; panning filters client-side, so the map is identical with or without signal (a banner shows the cache timestamp when offline). Markers are colored by modeled equity tier, ringed white when a scan exists, and drawn only below `MARKER_MAX_LAT_DELTA` zoom. Tapping shows the summary, then the cached row, then the live row — stale responses can never overwrite newer ones.
* **Capture** shoots up to 6 photos (UUID filenames, so deletes never collide), tags GPS + compass heading, writes everything to the app sandbox and an AsyncStorage queue, then uploads to `property-scans/<apn>/<scan_id>/<photo-id>.jpg` and upserts `property_scans`. Backing out without saving deletes the photos.
* **Upload queue** (`lib/offlineQueue.ts`): every mutation runs under a mutex; each photo tracks its own uploaded flag so a half-finished scan resumes where it stopped; network drops never consume an attempt, server rejections do, with exponential backoff (30 s → 30 min) and a manual *Retry now*. `flushQueue` distinguishes `offline` / `busy` / `not_signed_in` / `done`. `lib/__tests__/offlineQueue.test.ts` covers the interleaving, resume, backoff and parking cases; run with `npm test`.
* **LiDAR** is a native-module boundary (`lib/lidar.ts`): the steps to build an ARKit `ARMeshAnchor` Expo module are documented there; until it exists the button reports "unavailable" and photos still upload.
* Add a Google Maps key in `app.json` for Android; iOS uses Apple Maps by default.

## 4. Compliance & data posture

* **No MLS.** All parcel data comes from county GIS / assessor extracts; the schema has no MLS fields.
* **Fair housing by construction.** The firewall is deterministic and auditable; the vision prompt scores structures only; outreach rationale can cite only property, condition, financial, or zoning factors. Dossiers that trip a block, a warning, or a pipeline error land in `compliance_hold` for human review; only clean, error-free runs become `draft`.
* **Estimates are labeled.** `estimated_*` columns come from a naive Prop-13 model (`equity_model_version`). Sales under $25k (trust transfers, quitclaims, transfer-tax amounts) are ignored; parcels with no plausible sale get the assessed value as a low-confidence *floor*. Refinances/HELOCs are invisible to assessor data, so equity is an upper bound. Every report carries the "not an appraisal" disclaimer. Swap in a real AVM before outreach at scale.
* **Reports are sanitized.** `report_html` is passed through an allow-list sanitizer (`nh3`) and untrusted scalars are Markdown-escaped, so a sign in a photo or a dirty assessor string cannot inject script into a reviewer's browser.
* **PII minimization.** Owner names / mailing addresses are opt-in at ingest; photos are taken from the public right-of-way and stored in a private bucket.
* **Still on you:** TCPA / CAN-SPAM / California DNC rules for the outreach channel, a licensed-broker review of any solicitation, and a check of current ADU ordinances (county PDS, Escondido Planning) before quoting figures.

## 5. Roadmap after scaffold

- [ ] Spatial join against city-boundary layer to resolve `jurisdiction` instead of zoning-code heuristics
- [ ] Marker clustering above the zoom gate (currently a "zoom in" banner)
- [ ] On-device CV (blur/exposure gate) populating `edge_cv_metadata` before upload
- [ ] Supabase Database Webhook → `/webhooks/supabase/scan-uploaded`
- [ ] Human review UI for `compliance_hold` dossiers
