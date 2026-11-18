"""One-shot: merge compact metrics (with PAE matrix) into verify task rows."""
import json, os, sys

sys.path.insert(0, '/data/V-Bio')
os.environ.setdefault('VBIO_MONITOR_DATABASE_URL', 'postgresql://postgres:postgres@host.docker.internal:54322/postgres')
import psycopg2
from psycopg2.extras import Json

from backend.services.result_archive import ResultArchiveService

svc = ResultArchiveService.__new__(ResultArchiveService)
svc._compact_metrics_cache = {}
svc._compact_metrics_lock = __import__('threading').Lock()
svc.logger = __import__('logging').getLogger('backfill')

BATCH = int(sys.argv[1]) if len(sys.argv) > 1 else 500
OFFSET = int(sys.argv[2]) if len(sys.argv) > 2 else 0

conn = psycopg2.connect(os.environ['VBIO_MONITOR_DATABASE_URL'], application_name='vbio-pae-backfill')
conn.autocommit = True
cur = conn.cursor()
cur.execute("SET ROLE anon")
cur.execute(
    "SELECT task_id FROM project_tasks "
    "WHERE name LIKE 'RFD4 verify mol1%%' AND task_state='SUCCESS' "
    "ORDER BY created_at LIMIT %s OFFSET %s",
    (BATCH, OFFSET))
rows = [r[0] for r in cur.fetchall()]
cur.close()
print(f'{len(rows)} rows', flush=True)

ok = skip = 0
for tid in rows:
    zip_path = f'/data/boltz_central_results/{tid}_results.zip'
    if not os.path.exists(zip_path):
        skip += 1
        continue
    try:
        compact = svc._extract_compact_prediction_metrics_from_zip(zip_path)
        if not compact or 'pae_matrix' not in compact:
            skip += 1
            continue
        with conn.cursor() as cur:
            cur.execute("SET ROLE anon")
            cur.execute("SELECT confidence FROM project_tasks WHERE task_id = %s", (tid,))
            row = cur.fetchone()
            if row is None:
                skip += 1
                continue
            conf = row[0] if isinstance(row[0], dict) else {}
            conf.update(compact)
            cur.execute(
                "UPDATE project_tasks SET confidence = %s, updated_at = NOW() WHERE task_id = %s",
                (Json(conf), tid))
        ok += 1
    except Exception as exc:
        print('fail:', tid, str(exc)[:70], flush=True)
conn.close()
print(f'ok={ok} skip={skip}', flush=True)
