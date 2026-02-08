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

## 6) Notes

- Authentication uses lightweight `app_users.password_hash` (suitable for local `supabase-lite` workflow).
- New projects now start from workflow selection (prediction/designer/bicyclic/lead optimization/affinity).
- Prediction workflow supports multi-component input logic (protein/dna/rna/ligand, copies, ligand input mode).
- Multi-component input config is persisted per project in browser local storage (keyed by project ID).
- Queue/wait UX is improved with live status card, elapsed time, progress estimation, and auto-refresh.
