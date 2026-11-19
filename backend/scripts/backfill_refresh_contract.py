"""Refresh pass: re-extract compact metrics for multi-sample backends.

The earlier backfill picked per-sample artifacts by zip member order; the
extractor now ranks samples (af3 ranking_scores.csv full precision,
deterministic tie-break). Every alphafold3/protenix/mol1 SUCCESS row is
re-extracted and merge-overwritten into confidence. Pair ipSAE is stubbed
here and refreshed by backfill_refresh_ipsae.py.
"""
import json, os, sys, threading, logging
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, '/data/V-Bio')
DB_URL = os.environ.setdefault(
    'VBIO_MONITOR_DATABASE_URL',
    'postgresql://postgres:postgres@127.0.0.1:54322/postgres')
import psycopg2

from backend.services.result_archive import ResultArchiveService

logging.basicConfig(level=logging.CRITICAL)
svc = ResultArchiveService.__new__(ResultArchiveService)
svc._compact_metrics_cache = {}
svc._compact_metrics_lock = threading.Lock()
svc.logger = logging.getLogger('backfill')
svc._extract_pair_ipsae = lambda _zip: None

local = threading.local()


def db():
    if not hasattr(local, 'conn'):
        local.conn = psycopg2.connect(DB_URL, application_name='vbio-refresh')
        local.conn.autocommit = True
    return local.conn


conn = psycopg2.connect(DB_URL, application_name='vbio-refresh-plan')
cur = conn.cursor()
cur.execute("SET ROLE anon")
cur.execute(
    "SELECT task_id FROM project_tasks "
    "WHERE backend IN ('alphafold3', 'protenix', 'mol1') "
    "AND task_state='SUCCESS' ORDER BY created_at")
rows = [r[0] for r in cur.fetchall()]
cur.close()
conn.close()
print(f'{len(rows)} rows', flush=True)

lock = threading.Lock()
done = [0]
counts = {}


def refresh(tid):
    zip_path = f'/data/boltz_central_results/{tid}_results.zip'
    try:
        if not os.path.exists(zip_path):
            return 'missing'
        compact = svc._extract_compact_prediction_metrics_from_zip(zip_path)
        if not compact:
            return 'empty'
        payload = json.dumps(compact, ensure_ascii=False)
        conn = db()
        with conn.cursor() as cur:
            cur.execute("SET ROLE anon")
            cur.execute(
                "UPDATE project_tasks SET confidence = COALESCE(confidence, '{}'::jsonb) "
                "|| %s::jsonb WHERE task_id = %s",
                (payload, tid))
        return 'ok'
    except Exception as exc:
        print('fail:', tid, str(exc)[:120], flush=True)
        return 'fail'


with ThreadPoolExecutor(max_workers=8) as pool:
    for status in pool.map(refresh, rows):
        with lock:
            counts[status] = counts.get(status, 0) + 1
            done[0] += 1
            if done[0] % 200 == 0 or done[0] == len(rows):
                print(f'{done[0]}/{len(rows)} {counts}', flush=True)
