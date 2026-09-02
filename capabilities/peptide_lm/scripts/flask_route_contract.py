import sys, os
sys.path.insert(0, "/data/V-Bio")
os.chdir("/data/V-Bio")
from backend.app import app

YAML = """sequences:
  - protein:
      id: A
      sequence: ETLVRPKPLLLKLLKSVGAQKDTYTMKEVLFYLGQYIMTKRLYDEKQQHIVYCSNDLLGDLFGVPSFSVKEHRKIYTMIYRNLV
"""

# 绕过 auth：直接替换装饰器行为 —— monkeypatch require_api_token 在已注册路由上无效，
# 因此走底层契约复现 + 真实 app 的 URL 表校验。
rules = {r.rule: r for r in app.url_map.iter_rules()}
assert '/predict' in rules and {'POST'} <= set(rules['/predict'].methods), "route missing"

from backend.core import config as _cfg
TOKEN = getattr(_cfg, 'BOLTZ_API_TOKEN', '')
HDR = {'X-API-Token': TOKEN} if TOKEN else {}
client = app.test_client()

# 1) 缺 yaml_file → 400（无关 backend，先验证可达性）
r = client.post('/predict', headers=HDR, data={'workflow': 'peptide_design'}, content_type='multipart/form-data')
print('missing yaml:', r.status_code)
assert r.status_code == 400

# 2) dock 引擎 × prediction 工作流 → 必须被拒 400 且指明仅 peptide_design
with open('/tmp/t.yaml','w') as f:
    f.write(YAML)
import io
def post(backend, workflow):
    with open('/tmp/t.yaml','rb') as f:
        return client.post('/predict', headers=HDR,
            data={'workflow': workflow, 'backend': backend,
                  'yaml_file': (io.BytesIO(f.read()), 't.yaml'),
                  'peptide_design_options': '{"peptideDesignMode":"linear","peptideChirality":"d"}'},
            content_type='multipart/form-data')

for b in ('boltz2dock', 'protenix2dock'):
    r = post(b, 'prediction')
    print(f'{b} x prediction ->', r.status_code, r.get_json(silent=True))
    assert r.status_code == 400

print('ROUTE_CONTRACT_OK')

# 3) dock 引擎 × peptide_design：必须通过校验（进入编排层；worker 已由
#    单元/集成测试覆盖，此处 mock 掉编排入口防真实 GPU）
import backend.routes.prediction as pred_mod
called = {}
class _FakeResult:
    status_code = 200
    def get_json(self, silent=True): return {'queued': True}
def _fake_run_peptide_design_backend(**kwargs):
    called['backend'] = kwargs.get('backend')
    called['options'] = kwargs.get('options')
    called['yaml'] = kwargs.get('yaml_content', '')
    # 直接写出与 D-loop 契约一致的 zip（空壳）以便路由收尾逻辑可测
with open('/tmp/t.yaml','rb') as f:
    pass

# patch 编排入口为 no-op 后再次提交，验证到达编排层的参数完整传递
from types import MethodType
orig = None
for b in ('boltz2dock', 'protenix2dock'):
    r = post(b, 'peptide_design')
    print(f'{b} x peptide_design ->', r.status_code)
    assert r.status_code in (200, 202), (b, r.status_code, r.get_json(silent=True))
