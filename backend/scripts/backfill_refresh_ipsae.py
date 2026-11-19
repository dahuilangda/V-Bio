"""Refresh pass: recompute pair ipSAE for rows that already carry it.

Runs after backfill_refresh_contract.py: af3 sample selection now reads
ranking_scores.csv full precision, and the het-chain atom scan / cif letter
mapping were corrected, so existing pair_ipsae blocks may come from the
wrong sample or mis-bound chain letters.
"""
import json, os, sys, threading, logging, zipfile
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

local = threading.local()


def db():
    if not hasattr(local, 'conn'):
        local.conn = psycopg2.connect(DB_URL, application_name='vbio-ipsae-refresh')
        local.conn.autocommit = True
    return local.conn


conn = psycopg2.connect(DB_URL, application_name='vbio-ipsae-refresh-plan')
cur = conn.cursor()
cur.execute("SET ROLE anon")
cur.execute(
    "SELECT task_id FROM project_tasks "
    "WHERE backend IN ('boltz', 'alphafold3', 'protenix', 'mol1') "
    "AND task_state='SUCCESS' AND confidence ? 'pair_ipsae' "
    "ORDER BY created_at")
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
        with zipfile.ZipFile(zip_path) as zf:
            pair_block = svc._extract_pair_ipsae(zf)
        if not pair_block or 'pair_ipsae' not in pair_block:
            return 'nopair'
        payload = json.dumps(pair_block, ensure_ascii=False)
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
            if done[0] % 100 == 0 or done[0] == len(rows):
                print(f'{done[0]}/{len(rows)} {counts}', flush=True)
