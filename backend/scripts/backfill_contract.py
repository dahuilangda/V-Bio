"""One-shot: fast-contract backfill for structure-prediction task rows.

Merges PAE matrix + chain bounds + MSA depth + interface Sc + normalized
scalars into project_tasks.confidence for every SUCCESS row whose archive
carries the data, skipping rows already backfilled. The expensive per-pair
ipSAE CLI pass runs separately.
"""
import json, os, sys, threading, time

sys.path.insert(0, '/data/V-Bio')
os.environ.setdefault('VBIO_MONITOR_DATABASE_URL', 'postgresql://postgres:postgres@host.docker.internal:54322/postgres')
import psycopg2

from backend.services.result_archive import ResultArchiveService

svc = ResultArchiveService.__new__(ResultArchiveService)
svc._compact_metrics_cache = {}
svc._compact_metrics_lock = threading.Lock()
svc.logger = __import__('logging').getLogger('backfill')

conn = psycopg2.connect(os.environ['VBIO_MONITOR_DATABASE_URL'], application_name='vbio-contract-backfill')
conn.autocommit = True
cur = conn.cursor()
cur.execute("SET ROLE anon")
cur.execute(
    "SELECT task_id FROM project_tasks "
    "WHERE backend IN ('boltz', 'alphafold3', 'protenix', 'mol1', 'esmfold2') "
    "AND task_state='SUCCESS' AND NOT confidence ? 'pae_matrix' "
    "ORDER BY created_at")
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
        if not compact:
            skip += 1
            continue
        payload = json.dumps(compact, ensure_ascii=False).replace("'", "''")
        with conn.cursor() as cur:
            cur.execute("SET ROLE anon")
            cur.execute(
                "UPDATE project_tasks SET confidence = COALESCE(confidence, '{}'::jsonb) "
                "|| %s::jsonb, updated_at = NOW() WHERE task_id = %s",
                (payload, tid))
        ok += 1
    except Exception as exc:
        print('fail:', tid, str(exc)[:80], flush=True)
conn.close()
print(f'ok={ok} skip={skip}', flush=True)
