"""Slow pass: add pair_ipsae to rows that have PAE but not ipSAE."""
import sys, threading, logging
sys.path.insert(0, '/data/V-Bio')
import psycopg2, json
from backend.services.result_archive import ResultArchiveService

svc = ResultArchiveService.__new__(ResultArchiveService)
svc._compact_metrics_cache = {}
svc._compact_metrics_lock = threading.Lock()
svc.logger = __import__('logging').getLogger('backfill')

conn = psycopg2.connect('postgresql://postgres:postgres@host.docker.internal:54322/postgres', application_name='vbio-ipsae-backfill')
conn.autocommit = True
cur = conn.cursor()
cur.execute("SET ROLE anon")
cur.execute(
    "SELECT task_id FROM project_tasks "
    "WHERE backend IN ('boltz','alphafold3','protenix','mol1') "
    "AND task_state='SUCCESS' AND confidence ? 'pae_matrix' "
    "AND NOT confidence ? 'pair_ipsae' ORDER BY created_at")
rows = [r[0] for r in cur.fetchall()]
cur.close()
print(f'{len(rows)} rows', flush=True)

ok = fail = 0
for tid in rows:
    z = f'/data/boltz_central_results/{tid}_results.zip'
    try:
        import zipfile
        with zipfile.ZipFile(z) as zf:
            pair_block = svc._extract_pair_ipsae(zf)
        if not pair_block or 'pair_ipsae' not in pair_block:
            fail += 1
            continue
        payload = json.dumps(pair_block, ensure_ascii=False)
        with conn.cursor() as cur:
            cur.execute("SET ROLE anon")
            cur.execute(
                "UPDATE project_tasks SET confidence = COALESCE(confidence, '{}'::jsonb) "
                "|| %s::jsonb WHERE task_id = %s",
                (payload, tid))
        ok += 1
    except Exception as exc:
        print('fail:', tid, str(exc)[:80], flush=True)
        fail += 1
    if (ok + fail) % 50 == 0:
        print(f'{ok+fail}/{len(rows)} ok={ok}', flush=True)
conn.close()
print(f'done ok={ok} fail={fail}', flush=True)
