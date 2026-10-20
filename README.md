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
│   │   ├── load_parcels.py        # GeoPandas → Supabase bulk upsert (any parcel file)
│   │   ├── study_area.py          # public ArcGIS layers → parcels with overlay facts joined
│   │   ├── load_study_area.py     # CLI: fetch + join + upsert the real study area
│   │   ├── migrations/            # incremental SQL for existing databases
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

**Load the real study area** (7,121 parcels in the Harmony Grove bbox, overlays joined):
```bash
psql "$DATABASE_URL" -f ingestion/migrations/20261020_parcel_overlays.sql   # once, on databases created before Oct 20, 2026
python -m ingestion.load_study_area --dry-run                                # fetch + join + print counts
python -m ingestion.load_study_area --cache-dir .cache/study                 # upsert on APN; keeps raw layers for re-runs
```
Or run the **Load study area** workflow from the Actions tab (needs `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` repository secrets; dry run by default). `ingestion/study_area.py` fetches each layer from its public ArcGIS service and joins it:

| Column | Source | Join |
|---|---|---|
| `jurisdiction` | SANDAG municipal boundaries (parcel's own `situs_juris` as fallback) | representative point |
| `zoning_code` / `zoning_desc` | Escondido zoning (by APN); County zoning (unincorporated) | APN / representative point |
| `fire_hazard_zone`, `fire_responsibility` | CAL FIRE FHSZ: SRA map (eff. 2024-04-01) + LRA map (2025-03-24) | class covering the largest share of the lot |
| `fire_agency` | SANDAG fire agencies | representative point |
| `sewer_service` | County DEH septic/sewer designation | APN |
| `hoa`, `hoa_basis` | the lot's recorded subdivision contains a $0 common-area lot | subdivision |

`overlay_facts` keeps what each join measured: per-class fire shares and the highest class touching ≥5% of the lot, the raw DEH designation and confidence, both jurisdiction readings, and source dates. Two cautious choices: DEH's weakest call ("Assumed Septic Connected") is kept in `overlay_facts` but not treated as septic, and `hoa` is `true` or `null`, never `false`, because no public record proves a lot has no HOA. The source layer has no owner names; a last-document date is kept as `last_sale_date` only when the document is a grant deed, and document-type codes are never stored. Parcel and boundary data: *GIS data derived from SanGIS*.

**Ingest any other parcel file** (SanGIS `PARCELS` shapefile or any GeoJSON):
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
| `ZoningAnalyzerNode` | deterministic | Layered rules (`agents/rule_layers.py`) → feasibility score, caveats and a "who sets the rules" list. See below. |
| `DraftDossierNode` | template | Jinja2 Markdown from structured state only — no free-form prose. This is the canonical, screened text. |
| `ComplianceFirewallNode` | deterministic | Regex rule set for FHA + CA FEHA protected classes, steering phrases, distress targeting, occupant inference. Text is NFKC-normalized and stripped of invisible characters and HTML first. Blocks are redacted (overlapping spans merged), warnings route the dossier to `compliance_hold`, Equal Housing + estimate disclaimers appended. Lead rationale keys are whitelisted to property factors. Vision observations are screened by the same rules *before they enter state*, so no unscreened model text reaches the DB, API, or report. `tests/test_firewall_corpus.py` is the must-block / must-pass corpus — add a case whenever the field finds a miss. |

### Layered zoning rules

`agents/rule_layers.py` resolves every ADU rule for a parcel by layer, in order of precedence:

1. **State law**: floors local rules can't go below (an 800 sq ft ADU, 16 ft height), ceilings they can't exceed (4 ft side and rear setbacks), and restrictions it voids (sprinkler mandates, HOA bans). It wins any conflict.
2. **One local land-use authority**: the County of San Diego for unincorporated parcels *or* the City of Escondido, never both. It applies where state law leaves room.
3. **Overlays that stack**: the fire authority, County Environmental Health (septic) and any HOA. All apply together, except where state law voids a requirement.

Each rule records its source, effective date and status: `settled`, `verify`, `unsettled` (the agencies disagree, or the rule was withdrawn, so Vecterra says so instead of picking a side), `upcoming`, or `not_checked` (a parcel fact not loaded yet). Results depend on `as_of`, so AB 956's two detached ADUs switch on automatically on Jan 1, 2027, and an unupdated local ordinance is recorded as a resolved conflict. From that date, a lot that builds two detached ADUs need not also get a junior ADU, and the HOA limits in Civil Code § 4751 reach any lot zoned to allow a single-family home, including planned developments (AB 956, Ch. 791). Every residential parcel also gets an AB 2533 note (Gov. Code § 66332): a unit built before 2020 without permits can be permitted, and since neither state law nor the County or Escondido defines proof of the build date, the note lists the types other agencies publish. Local caps come from the published codes: the County's 1,200 sq ft and 24 ft (PDS-611), and Escondido's 1,000 sq ft (850 sq ft for one bedroom on lots under 20,000 sq ft), zone height (35 ft in R-1, R-E and R-A) and 4 ft side and rear setbacks (§§ 33-1474, 33-107). Overlay facts (`fire_hazard_zone`, `fire_responsibility`, `fire_agency`, `sewer_service`, `hoa`, `hoa_basis`) come from `ingestion/load_study_area.py`; an HOA inferred from records is reported as `verify`, not `settled`. Parcels in the City of San Marcos or City of San Diego are named as such, with only state law applied until their ordinances are modeled. This is a working model of the law, not legal advice; `verify` items say who to confirm with.

### The dossier report

`agents/report_html.py` renders the homeowner-facing report from the same structured state: the parcel drawn from its PostGIS geometry as a survey plat (boundary lengths, scale bar, north arrow, setback line when visible at scale) on the brand's night background, then exterior condition, lot and ADU potential, estimated value and equity, the property factors behind the outreach, and next steps. It is responsive and prints to US Letter.

After rendering, the compliance firewall screens the report's visible text as well (the fixed disclaimer block excepted). If anything is blocked, the pipeline falls back to HTML rendered from the redacted Markdown, so an unscreened sentence can never reach a reader. `model_versions.report` records which one was used.

## 3. Mobile

Expo SDK 58 · React Native 0.88 · Expo Router · TypeScript. Requires **Node 22.13+** (`mobile/.nvmrc`; `nvm use` picks it up).

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

- [x] Spatial join against city-boundary layer to resolve `jurisdiction` instead of zoning-code heuristics (`ingestion/load_study_area.py`)
- [ ] Real lot geometry for ADU room (building footprints, setbacks, slope) instead of the square-lot approximation in `zoning_rules.py`
- [ ] Marker clustering above the zoom gate (currently a "zoom in" banner)
- [ ] On-device CV (blur/exposure gate) populating `edge_cv_metadata` before upload
- [ ] Supabase Database Webhook → `/webhooks/supabase/scan-uploaded`
- [ ] Human review UI for `compliance_hold` dossiers
