# V-Bio 前端（frontend/）

React + Vite V-Bio frontend with:

- Project management (create, search, edit, soft-delete, detail view)
- User management (sign in, register, admin user management)
- Molecule editing with JSME
- Protein structure visualization with Mol* (default AlphaFold pLDDT-like coloring)
- Runtime integration with backend APIs:
  - `POST /predict`
  - `GET /status/:task_id`
  - `GET /results/:task_id`
- Persistence through `supabase-lite` (Postgres + PostgREST)
- Project/task Copilot chat for collaboration, analysis, confirmed task actions, and Affinity file upload assistance

## 0) Prepare Python venv (required by management API)

`frontend/run.sh` and `vbio_management_api.py` require a Python virtual environment.
By default, `run.sh` looks for:

- `/data/V-Bio/venv`
- `frontend/venv`

Recommended setup:

```bash
cd /data/V-Bio
python3 -m venv venv
source ./venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

## 1) Start Supabase-lite

```bash
cd frontend/supabase-lite
docker compose up -d
```

Default PostgREST URL: `http://127.0.0.1:54321`

For a clean install, the full schema is created directly from `frontend/supabase-lite/init/init.sql`
when the Postgres volume is initialized.

If you are upgrading an existing `supabase-lite` Postgres volume, re-run the idempotent init SQL once so Copilot
message tables are available to PostgREST:

```bash
cd frontend/supabase-lite
docker compose exec -T db psql -U postgres -d postgres -f /docker-entrypoint-initdb.d/01-init.sql
docker compose exec -T db psql -U postgres -d postgres -c "notify pgrst, 'reload schema';"
```

The Copilot chat table added by this schema is:

- `project_copilot_messages`: unified Copilot chat on project list, task list, and task detail pages.

One-command startup for production-style frontend stack:

```bash
cd /data/V-Bio
bash frontend/run.sh start
```

Useful runtime commands:

```bash
bash frontend/run.sh status
bash frontend/run.sh stop
bash frontend/run.sh dev
```

## 2) Configure environment variables

```bash
cd frontend
cp .env.example .env
```

Important variables:

- `VITE_API_BASE_URL`: backend API base URL. Leave empty to use Vite proxy (defaults to `http://127.0.0.1:5000`).
- `VITE_API_TOKEN`: must match backend `BOLTZ_API_TOKEN`.
- `VITE_SUPABASE_REST_URL`: PostgREST endpoint, default `http://127.0.0.1:54321`.
- `VITE_VBIO_MANAGEMENT_API_BASE_URL`: optional browser-visible management API base URL. Leave empty to use the Vite `/vbio-api` proxy during local development.
- `VBIO_COPILOT_API_URL`: server-side OpenAI-compatible chat completions endpoint for Copilot.
- `VBIO_COPILOT_API_KEY`: server-side bearer token for the chat endpoint.
- `VBIO_COPILOT_MODEL`: model name used by Copilot, for example `gemma4-31b`.

Copilot is enabled only when the management API process has `VBIO_COPILOT_API_URL` in its environment.
The browser bundle does not read Copilot keys directly. If you start the app with `frontend/run.sh`, put the
Copilot variables in `frontend/.env`; that script loads the file before starting `vbio_management_api:app`.

## 3) Run V-Bio frontend

```bash
cd frontend
npm install
npm run dev
```

Default URL: `http://127.0.0.1:5173`

The frontend uses `react-markdown` and `remark-gfm` for Copilot message rendering. They are included in
`package.json`; `npm install` is enough on a fresh checkout.

## 4) Create/upgrade admin user (CLI)

```bash
cd frontend
npm run create-admin -- --username admin --password 'YourPassword' --name 'System Admin' --email admin@example.com
```

Optional:

- `--rest-url http://127.0.0.1:54321`

If the user exists, the script upgrades it to admin and updates the password hash.

## 5) User CLI

```bash
cd frontend

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

## 6) Start V-Bio Management API (gateway)

This gateway is the V-Bio frontend layer for cURL submission and project-scoped token auth.
It forwards runtime requests to V-Bio using an internal backend token.
Original V-Bio API behavior is not modified.

```bash
cd /data/V-Bio
source ./venv/bin/activate

export VBIO_POSTGREST_URL="http://127.0.0.1:54321"
export VBIO_RUNTIME_API_BASE_URL="http://127.0.0.1:5000"
export VBIO_RUNTIME_API_TOKEN="<BOLTZ_BACKEND_TOKEN>"
export VBIO_COPILOT_API_URL="http://219.146.211.42:29568/v1/chat/completions"
export VBIO_COPILOT_API_KEY="<COPILOT_API_KEY>"
export VBIO_COPILOT_MODEL="gemma4-31b"

python ./frontend/server/vbio_management_api.py
```

Default gateway URL: `http://127.0.0.1:5055/vbio-api`

To verify Copilot configuration after restarting the management API:

```bash
curl http://127.0.0.1:5055/vbio-api/copilot/config
# expected: {"enabled":true}
```

Legacy `VBIO_TASK_CHAT_API_URL`, `VBIO_TASK_CHAT_API_KEY`, and `VBIO_TASK_CHAT_MODEL` are still accepted as a
compatibility fallback, but new deployments should use the `VBIO_COPILOT_*` names.

Task rows include a shared chat. Mention `@V-Bio Copilot` in a task chat message to route the question through the
management API, which adds task context and persists the assistant reply.

Project list, task list, and task detail pages also include a floating Copilot button in the lower-right corner.
Copilot can answer read-only analysis questions immediately. Actions that change filters, patch parameters, save
drafts, or submit work are planned first and require an explicit confirmation button in the UI.

After changing Copilot backend code, restart the management API process. With `frontend/run.sh`:

```bash
bash frontend/run.sh stop
bash frontend/run.sh start
```

Or restart only the gunicorn/process manager that serves `vbio_management_api:app`.

### Copilot task-input behavior

Copilot validates the current workflow before planning a submission:

- Structure Prediction requires valid structural components.
- Affinity Scoring requires an affinity-ready target/ligand setup. A single peptide/protein sequence is not enough for
  an Affinity task.
- Peptide Designer requires target/design intent and design options.
- Lead Optimization uses dedicated candidate/MMP tools, not generic parameter patching.

Copilot file input uses a compact `+` button inside the chat composer. Uploaded files appear as `@filename` chips,
and clicking a chip inserts the mention into the message. The model uses the surrounding text and `@filename`
mentions to infer file roles:

- Structure Prediction: PDB/CIF/MMCIF files are templates only when the user explicitly says template/模板.
- Affinity Scoring: target/protein/receptor files map to the target, and ligand/small-molecule/compound files map to
  the ligand.
- Peptide Designer: target/protein/receptor files map to the peptide-design target structure.
- Lead Optimization: target/protein/receptor files map to reference target context, and ligand/compound files map to
  ligand context.

If file roles are unclear, Copilot should ask a follow-up question instead of guessing.

## 7) Local submit with cURL

1. Sign in to V-Bio web UI.
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
- Projects are created in V-Bio web UI (not via API).
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
