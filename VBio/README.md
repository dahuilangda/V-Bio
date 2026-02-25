# VBio (V-Bio React)

React + Vite frontend with:

- Project management (create, search, edit, soft-delete, detail view)
- User management (sign in, register, admin user management)
- Molecule editing with JSME
- Protein structure visualization with Mol* (default AlphaFold pLDDT-like coloring)
- Runtime integration with backend APIs:
  - `POST /predict`
  - `GET /status/:task_id`
  - `GET /results/:task_id`
- Persistence through `supabase-lite` (Postgres + PostgREST)

## 1) Start Supabase-lite

```bash
cd VBio/supabase-lite
docker compose up -d
```

Default PostgREST URL: `http://127.0.0.1:54321`

If you pulled new VBio code on an existing Postgres volume, run schema upgrade once:

```bash
cd VBio
npm run db:migrate
```

One-command startup for production-style VBio stack:

```bash
cd /data/Boltz-WebUI
bash VBio/run.sh start
```

Useful runtime commands:

```bash
bash VBio/run.sh status
bash VBio/run.sh stop
bash VBio/run.sh dev
```

## 2) Configure environment variables

```bash
cd VBio
cp .env.example .env
```

Important variables:

- `VITE_API_BASE_URL`: backend API base URL. Leave empty to use Vite proxy (defaults to `http://127.0.0.1:5000`).
- `VITE_API_TOKEN`: must match backend `BOLTZ_API_TOKEN`.
- `VITE_SUPABASE_REST_URL`: PostgREST endpoint, default `http://127.0.0.1:54321`.

## 3) Run frontend

```bash
cd VBio
npm install
npm run dev
```

Default URL: `http://127.0.0.1:5173`

## 4) Create/upgrade admin user (CLI)

```bash
cd VBio
npm run create-admin -- --username admin --password 'YourPassword' --name 'System Admin' --email admin@example.com
```

Optional:

- `--rest-url http://127.0.0.1:54321`

If the user exists, the script upgrades it to admin and updates the password hash.

## 5) User CLI

```bash
cd VBio

# list users
npm run users -- list

# include deleted users
npm run users -- list --include-deleted

# soft-delete by username
npm run users -- delete --username alice

# hard-delete by id
npm run users -- delete --id <uuid> --hard

# reset password
npm run users -- set-password --username admin --password 'NewPassword123'

# toggle admin role
npm run users -- set-admin --username alice --value true
```

Optional for all commands:

- `--rest-url http://127.0.0.1:54321`

## 6) Start VBio Management API (gateway)

This gateway is the VBio layer for cURL submission and project-scoped token auth.
It forwards runtime requests to Boltz-WebUI using an internal backend token.
Original Boltz-WebUI API behavior is not modified.

```bash
cd /data/Boltz-WebUI
source ./venv/bin/activate

export VBIO_POSTGREST_URL="http://127.0.0.1:54321"
export VBIO_RUNTIME_API_BASE_URL="http://127.0.0.1:5000"
export VBIO_RUNTIME_API_TOKEN="<BOLTZ_BACKEND_TOKEN>"

python ./VBio/server/vbio_management_api.py
```

Default gateway URL: `http://127.0.0.1:5055/vbio-api`

## 7) Local submit with cURL

1. Sign in to VBio web UI.
2. Open `API Access`.
3. Create API token and copy it (shown once).
4. Run cURL from your local machine where your input files are stored.

```bash
export VBIO_API_BASE="http://127.0.0.1:5055/vbio-api"
export VBIO_API_TOKEN="<YOUR_PROJECT_TOKEN>"
export VBIO_PROJECT_ID="<PROJECT_UUID>"
```

Predict with local YAML:

```bash
curl -X POST "${VBIO_API_BASE}/predict" \
  -H "X-API-Token: ${VBIO_API_TOKEN}" \
  -F "project_id=${VBIO_PROJECT_ID}" \
  -F "task_name=Predict task (optional)" \
  -F "task_summary=Short summary (optional)" \
  -F "yaml_file=@./config.yaml" \
  -F "backend=boltz"
```

Affinity score (Boltz2Score) with local target/ligand files:

```bash
curl -X POST "${VBIO_API_BASE}/api/boltz2score" \
  -H "X-API-Token: ${VBIO_API_TOKEN}" \
  -F "project_id=${VBIO_PROJECT_ID}" \
  -F "protein_file=@./target.cif" \
  -F "ligand_file=@./ligand.sdf" \
  -F "backend=boltz"
```

Cancel task:

```bash
curl -X DELETE "${VBIO_API_BASE}/tasks/<TASK_ID>?project_id=${VBIO_PROJECT_ID}&operation_mode=cancel" \
  -H "X-API-Token: ${VBIO_API_TOKEN}"
```

Check task status:

```bash
curl -X GET "${VBIO_API_BASE}/status/<TASK_ID>?project_id=${VBIO_PROJECT_ID}" \
  -H "X-API-Token: ${VBIO_API_TOKEN}"
```

Download task result:

```bash
curl -X GET "${VBIO_API_BASE}/results/<TASK_ID>?project_id=${VBIO_PROJECT_ID}" \
  -H "X-API-Token: ${VBIO_API_TOKEN}" \
  -o ./result.zip
```

Notes:
- Projects are created in VBio web UI (not via API).
- Each token is bound to one project and permissions (`submit/delete/cancel`).
- cURL requests must include `project_id`; gateway stores task snapshot into that project.
- API token lifecycle and usage analytics are in `API Access`.
- Runtime backend still uses its original token/auth config.
- Prediction `backend` can be `boltz`, `alphafold3`, or `protenix`.
- Prediction MSA behavior should be declared in YAML (`protein.msa: empty` disables external MSA generation).

## 8) Notes

- Authentication uses lightweight `app_users.password_hash` (suitable for local `supabase-lite` workflow).
- New projects now start from workflow selection (prediction/designer/bicyclic/lead optimization/affinity).
- Prediction workflow supports multi-component input logic (protein/dna/rna/ligand, copies, ligand input mode).
- Multi-component input config is persisted per project in browser local storage (keyed by project ID).
- Queue/wait UX is improved with live status card, elapsed time, progress estimation, and auto-refresh.
