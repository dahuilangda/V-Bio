"""Refresh pair ipSAE and interface Sc with binder/receptor grouping."""
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

from backend.services.result_archive import ResultArchiveService, _binding_record

logging.basicConfig(level=logging.CRITICAL)
svc = ResultArchiveService.__new__(ResultArchiveService)
svc._compact_metrics_cache = {}
svc._compact_metrics_lock = threading.Lock()
svc.logger = logging.getLogger('backfill')

PAIR_KEYS = ('pair_ipsae', 'ipsae_protein_to_ligand', 'ipsae_ligand_to_protein', 'ipsae_official_max')
LEGACY_KEYS = {
    'protein_to_ligand_ipsae': 'ipsae_protein_to_ligand',
    'ligand_to_protein_ipsae': 'ipsae_ligand_to_protein',
    'ligand_ipsae_max': 'ipsae_official_max',
}
SC_KEYS = ('interface_sc', 'interface_sc_median')

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
    "SELECT task_id, confidence, properties, constraints FROM project_tasks "
    "WHERE backend IN ('boltz', 'alphafold3', 'protenix', 'mol1', 'esmfold2') "
    "AND task_state='SUCCESS' AND confidence ? 'pair_ipsae' "
    "ORDER BY created_at")
rows = cur.fetchall()
cur.close()
conn.close()
print(f'{len(rows)} rows', flush=True)

lock = threading.Lock()
done = [0]
counts = {}


def refresh(row):
    tid, stored, properties, constraints = row
    zip_path = f'/data/boltz_central_results/{tid}_results.zip'
    try:
        if not os.path.exists(zip_path):
            return 'missing'
        with zipfile.ZipFile(zip_path) as zf:
            binding = svc._input_binding(zf) or _binding_record(properties, constraints)
            pair_block = svc._extract_pair_ipsae(zf, binding)
            sc_block = svc._extract_interface_sc(zf, binding)
        payload, drop = {}, []
        if pair_block is not None and 'pair_ipsae' in pair_block:
            payload.update({key: pair_block[key] for key in PAIR_KEYS if pair_block.get(key) is not None})
            payload.update({old: pair_block[new] for old, new in LEGACY_KEYS.items()
                            if pair_block.get(new) is not None})
            drop += list(PAIR_KEYS)
        if sc_block is not None:
            payload.update(sc_block)
            drop += list(SC_KEYS)
        if not drop:
            return 'empty'
        if all(stored.get(key) == payload.get(key) for key in drop):
            return 'same'
        conn = db()
        with conn.cursor() as cur:
            cur.execute("SET ROLE anon")
            cur.execute(
                "UPDATE project_tasks SET confidence = "
                "(COALESCE(confidence, '{}'::jsonb) - %s::text[]) || %s::jsonb WHERE task_id = %s",
                (drop, json.dumps(payload, ensure_ascii=False), tid))
        return 'updated'
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
