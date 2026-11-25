"""Restore protein modifications on task rows from the archived engine input."""
import json
import os
import sys
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, '/data/V-Bio')
sys.path.insert(0, '/data/V-Bio/frontend/server')
DB_URL = os.environ.setdefault(
    'VBIO_MONITOR_DATABASE_URL',
    'postgresql://postgres:postgres@127.0.0.1:54322/postgres')
import psycopg2
import yaml
from management_api.task_snapshot import _parse_protein_modifications

conn = psycopg2.connect(DB_URL, application_name='vbio-task-modifications')
conn.autocommit = True
with conn.cursor() as cur:
    cur.execute("SET ROLE anon")
    cur.execute(
        "SELECT task_id, components FROM project_tasks "
        "WHERE task_state = 'SUCCESS' AND jsonb_typeof(components) = 'array'")
    rows = cur.fetchall()
conn.close()
print(f'{len(rows)} rows', flush=True)

local = threading.local()
lock = threading.Lock()
counts = {}


def db():
    if not hasattr(local, 'conn'):
        local.conn = psycopg2.connect(DB_URL, application_name='vbio-task-modifications')
        local.conn.autocommit = True
    return local.conn


def restore(row):
    task_id, components = row
    try:
        source = zipfile.ZipFile(f'/data/boltz_central_results/{task_id}_results.zip')
    except (OSError, zipfile.BadZipFile):
        return 'missing'
    records = sorted(name for name in source.namelist()
                     if name.count('/') == 1 and name.endswith('/input.yaml'))
    if not records:
        return 'no-record'
    record = yaml.safe_load(source.read(records[0]))
    sequences = record.get('sequences') if isinstance(record, dict) else None
    if not isinstance(sequences, list):
        return 'no-sequences'
    changed = False
    for index, item in enumerate(sequences):
        entry = item.get('protein') if isinstance(item, dict) else None
        if not isinstance(entry, dict):
            continue
        component_id = f'yaml-protein-{index + 1}'
        component = next((candidate for candidate in components
                          if isinstance(candidate, dict) and candidate.get('id') == component_id), None)
        if component is None or component.get('modifications'):
            continue
        sequence = ''.join(str(entry.get('sequence') or '').split())
        modifications = _parse_protein_modifications(entry, sequence, component_id)
        if modifications:
            component['modifications'] = modifications
            changed = True
    if not changed:
        return 'same'
    try:
        with db().cursor() as cur:
            cur.execute("SET ROLE anon")
            cur.execute("UPDATE project_tasks SET components = %s::jsonb WHERE task_id = %s",
                        (json.dumps(components, ensure_ascii=False), task_id))
    except Exception as exc:
        print('fail:', task_id, str(exc)[:120], flush=True)
        return 'fail'
    return 'updated'


with ThreadPoolExecutor(max_workers=16) as pool:
    for status in pool.map(restore, rows):
        with lock:
            counts[status] = counts.get(status, 0) + 1
print(counts, flush=True)
