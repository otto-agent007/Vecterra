<p align="center">
  <img src="mobile/assets/brand/vecterra-logo.png" alt="Vecterra" width="300">
</p>

# Vecterra

[![CI](https://github.com/otto-agent007/Vecterra/actions/workflows/ci.yml/badge.svg)](https://github.com/otto-agent007/Vecterra/actions/workflows/ci.yml)

An offline-first field tool that helps real estate agents find ADU and equity opportunities in Harmony Grove and Escondido, CA (92029). It uses public parcel records, photos the agent takes from the street, and the published ADU rules. It uses no MLS data and doesn't sell homeowner data.

```
public parcel + overlay layers ─► Supabase/PostGIS ─► mobile map ─► field photos (offline queue)
                                                                          │
                     homeowner dossier ◄─ fair-housing firewall ◄─ agents: vision · ADU rules
```

## Layout

| Path | What's there |
|---|---|
| `backend/ingestion/` | `schema.sql` (tables, RLS, storage), `migrations/`, `load_study_area.py` (real parcels + overlays), `load_parcels.py` (any parcel file) |
| `backend/agents/` | LangGraph pipeline: vision scoring, ADU rules (`rule_layers.py`, `zoning_rules.py`), compliance firewall, dossier and HTML report |
| `backend/api/` | FastAPI: `/pipeline/run` and the Supabase upload webhook |
| `backend/tests/` | pytest suites; `sql/` runs the schema and RLS assertions on a real PostGIS |
| `mobile/` | Expo app: sign-in, map, parcel card, photo capture, offline upload queue |

The rules, data sources and joins are documented at the top of each module.

## Setup

**Database.** Create a Supabase project, then apply the schema (or, for an existing database, the files in `backend/ingestion/migrations/`):
```bash
psql "$DATABASE_URL" -f backend/ingestion/schema.sql
```

**Load parcels.** Run the **Load study area** workflow from the Actions tab (needs `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` repo secrets; dry run by default), or locally:
```bash
cd backend && pip install -r requirements.txt && cp .env.example .env
python -m ingestion.load_study_area --dry-run   # fetch and join, print counts
python -m ingestion.load_study_area             # upsert ~7,100 parcels on APN
```

**Backend** (from `backend/`):
```bash
python -m agents.pipeline --demo                 # synthetic parcel, no network
uvicorn api.main:app --reload --port 8000        # needs VECTERRA_API_TOKEN unless VECTERRA_ENV=dev
pytest
DATABASE_URL=postgres://... tests/sql/run.sh     # schema + RLS checks on a scratch PostGIS
```

**Mobile** (Expo SDK 58, Node 22.13+):
```bash
cd mobile && npm install && cp .env.example .env
npx expo start                                   # open in Expo Go
npm test
```
LiDAR needs a development build (`npx expo run:ios`); see `lib/lidar.ts`. Android needs a Google Maps key in `app.json`.

## Ground rules

- **Not a data broker.** No lead sales. Owner names aren't loaded, and agents can't read owner or raw tax columns.
- **Locked down by default.** Anonymous users get nothing. Agents see only their own scans and photos. CI tests these rules against a real database.
- **Fair housing by design.** The vision model scores structures only. Every dossier passes a deterministic FHA/FEHA firewall, and anything it flags is held for human review.
- **Honest about certainty.** Each ADU rule is marked settled, verify, unsettled, upcoming or not checked, with its source. Equity figures are labeled estimates, not appraisals.
- **Attribution.** Parcel and boundary data: *GIS data derived from SanGIS*. Fire zones: CAL FIRE.
- **Still needed before outreach:** TCPA, CAN-SPAM and California do-not-call compliance for the channel used, and broker review of any solicitation.

## Roadmap

- [ ] Real lot shape for ADU room (footprints, setbacks, slope) instead of treating each lot as a square
- [ ] Scope dossiers to the agent who made the scan (any signed-in agent can read them today)
- [ ] Homeowner unlock: dossier details reach an agent only after the owner asks to be contacted
- [ ] Human review screen for held dossiers
- [ ] Supabase webhook wired to `/webhooks/supabase/scan-uploaded`
- [ ] Marker clustering when zoomed out
- [ ] On-device photo quality check before upload
