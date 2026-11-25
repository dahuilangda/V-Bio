"""Refresh per-residue pLDDT on task rows from the archived best structure."""
import json
import logging
import os
import sys
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, '/data/V-Bio')
DB_URL = os.environ.setdefault(
    'VBIO_MONITOR_DATABASE_URL',
    'postgresql://postgres:postgres@127.0.0.1:54322/postgres')
import psycopg2

from backend.services.result_archive import ResultArchiveService

logging.basicConfig(level=logging.CRITICAL)
svc = ResultArchiveService.__new__(ResultArchiveService)
svc.logger = logging.getLogger('backfill')

METRIC_KEYS = ('residue_plddt_by_chain', 'chain_mean_plddt')
BACKENDS = ('boltz', 'alphafold3', 'protenix', 'mol1', 'esmfold2')

conn = psycopg2.connect(DB_URL, application_name='vbio-residue-plddt-plan')
cur = conn.cursor()
cur.execute("SET ROLE anon")
cur.execute(
    "SELECT task_id, confidence FROM project_tasks "
    "WHERE backend = ANY(%s) AND task_state = 'SUCCESS' ORDER BY created_at", (list(BACKENDS),))
rows = cur.fetchall()
cur.close()
conn.close()
print(f'{len(rows)} rows', flush=True)

local = threading.local()
lock = threading.Lock()
counts = {}


def db():
    if not hasattr(local, 'conn'):
        local.conn = psycopg2.connect(DB_URL, application_name='vbio-residue-plddt')
        local.conn.autocommit = True
    return local.conn


def misaligned(stored, computed):
    stored_series = stored.get('residue_plddt_by_chain') if isinstance(stored, dict) else None
    if not isinstance(stored_series, dict):
        return True
    return any(len(stored_series.get(chain) or []) != len(values)
               for chain, values in computed['residue_plddt_by_chain'].items())


def refresh(row):
    tid, stored = row
    try:
        source = zipfile.ZipFile(f'/data/boltz_central_results/{tid}_results.zip')
    except (OSError, zipfile.BadZipFile):
        return 'missing'
    try:
        computed = svc._extract_residue_plddt_from_zip(source)
        if not computed or 'residue_plddt_by_chain' not in computed:
            return 'empty'
        if not misaligned(stored, computed):
            return 'same'
        with db().cursor() as cur:
            cur.execute("SET ROLE anon")
            cur.execute(
                "UPDATE project_tasks SET confidence = COALESCE(confidence, '{}'::jsonb) || %s::jsonb "
                "WHERE task_id = %s",
                (json.dumps(computed, ensure_ascii=False), tid))
        return 'updated'
    except Exception as exc:
        print('fail:', tid, str(exc)[:120], flush=True)
        return 'fail'


with ThreadPoolExecutor(max_workers=16) as pool:
    for status in pool.map(refresh, rows):
        with lock:
            counts[status] = counts.get(status, 0) + 1
print(counts, flush=True)

with db().cursor() as cur:
    cur.execute("SET ROLE anon")
    cur.execute(
        "UPDATE projects p SET confidence = (COALESCE(p.confidence, '{}'::jsonb) - %s::text[]) || COALESCE(("
        "SELECT jsonb_object_agg(key, value) FROM jsonb_each(t.confidence) WHERE key = ANY(%s::text[])), '{}'::jsonb) "
        "FROM project_tasks t WHERE t.task_id = p.task_id AND t.confidence ? 'residue_plddt_by_chain'",
        (list(METRIC_KEYS), list(METRIC_KEYS)))
    print(f'project snapshots synced: {cur.rowcount}', flush=True)
