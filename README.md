# Vecterra — Spatial & Structural Equity Engine (MVP)

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
3. Enable **Anonymous sign-ins** (Auth → Providers) for the MVP mobile client, or wire real accounts.

Tables: `parcels` (APN, situs, assessor values, modeled equity, `geometry(Polygon,4326)`), `property_scans` (device-generated UUID, storage paths, edge-CV JSON, capture point), `dossiers` (health scores, zoning JSON, compliance log, report Markdown/HTML). RPC `parcels_in_bbox` feeds the map. Bucket `property-scans` is private; access via RLS + signed URLs.

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
pytest                                              # firewall regression tests
```

**Serve the API:**
```bash
uvicorn api.main:app --reload --port 8000
curl -X POST localhost:8000/pipeline/run -H 'content-type: application/json' -d '{"scan_id":"<uuid>"}'
```
`POST /webhooks/supabase/scan-uploaded` accepts a Supabase Database Webhook on `property_scans` INSERT so dossiers generate automatically after a field upload.

### Agent graph

| Node | Type | What it does |
|---|---|---|
| `VisionScorerNode` | LLM (vision) | Scores roof / paint / structure 0–100 from signed photo URLs. System prompt forbids describing people or occupant signals. Model set by `VECTERRA_VISION_MODEL`. |
| `ZoningAnalyzerNode` | deterministic | State ADU floor (4 ft side/rear setbacks, 800 sq ft by-right) + county/city tables → feasibility score and caveats. Every number carries a `verify` note. |
| `DraftDossierNode` | template | Jinja2 Markdown from structured state only — no free-form prose. |
| `ComplianceFirewallNode` | deterministic | Regex rule set for FHA + CA FEHA protected classes, steering phrases, distress targeting, occupant inference. Blocks are redacted, warnings logged, Equal Housing + estimate disclaimers appended. Lead rationale keys are whitelisted to property factors. |

## 3. Mobile

```bash
cd mobile
npm install
npx expo install --fix          # align native module versions with the installed SDK
cp .env.example .env            # EXPO_PUBLIC_SUPABASE_URL / EXPO_PUBLIC_SUPABASE_ANON_KEY
npx expo run:ios                # dev client (react-native-maps + camera need native code; Expo Go won't do)
```

* **Map** opens on Harmony Grove (33.0935, −117.1175). Markers come from `parcels_in_bbox`, colored by modeled equity tier, ringed white when a scan exists. Tapping fetches the full `parcels` row; the last successful fetch is cached for offline use.
* **Capture** shoots up to 6 photos, tags GPS + compass heading, writes everything to the app sandbox and an AsyncStorage queue, then uploads to `property-scans/<apn>/<scan_id>/photo-N.jpg` and upserts `property_scans`. Uploads retry automatically on reconnect (`_layout.tsx`).
* **LiDAR** is a native-module boundary (`lib/lidar.ts`): the steps to build an ARKit `ARMeshAnchor` Expo module are documented there; until it exists the button reports "unavailable" and photos still upload.
* Add a Google Maps key in `app.json` for Android; iOS uses Apple Maps by default.

## 4. Compliance & data posture

* **No MLS.** All parcel data comes from county GIS / assessor extracts; the schema has no MLS fields.
* **Fair housing by construction.** The firewall is deterministic and auditable; the vision prompt scores structures only; outreach rationale can cite only property, condition, financial, or zoning factors. Dossiers that trip a block land in `compliance_hold` for human review.
* **Estimates are labeled.** `estimated_*` columns come from a naive Prop-13 uplift model (`equity_model_version`), and every report carries the "not an appraisal" disclaimer. Swap in a real AVM before outreach at scale.
* **PII minimization.** Owner names / mailing addresses are opt-in at ingest; photos are taken from the public right-of-way and stored in a private bucket.
* **Still on you:** TCPA / CAN-SPAM / California DNC rules for the outreach channel, a licensed-broker review of any solicitation, and a check of current ADU ordinances (county PDS, Escondido Planning) before quoting figures.

## 5. Roadmap after scaffold

- [ ] Spatial join against city-boundary layer to resolve `jurisdiction` instead of zoning-code heuristics
- [ ] Marker clustering (`react-native-map-clustering`) above ~300 parcels
- [ ] On-device CV (blur/exposure gate) populating `edge_cv_metadata` before upload
- [ ] Replace anonymous auth with field-agent accounts and per-agent RLS
- [ ] Supabase Database Webhook → `/webhooks/supabase/scan-uploaded`
- [ ] Human review UI for `compliance_hold` dossiers
