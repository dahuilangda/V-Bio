"""标准配置文件契约：代码读取的每个部署配置键必须在模板中登记。

configs/vbio.example.toml 是配置键目录（唯一事实源）。新增 os.environ.get
而模板缺失会在此失败——防止配置面发散回"到处翻代码找 env"的状态。
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "configs" / "vbio.example.toml"

KEY_RE = re.compile(r"""os\.environ\.get\(\s*["']([A-Z_][A-Z_0-9]*)["']""")

# 运行期注入键：worker/子进程启动时写入，非部署配置。
# CELERY_WORKER_*/HOSTNAME 由入口脚本按实例生成；VBIO_API_URL 由任务代码
# 从 config 派生后注入子进程。
RUNTIME_INJECTED = {
    "VBIO_TASK_ID",
    "VBIO_PARENT_TASK_ID",
    "VBIO_PEPTIDE_WORKER_TASK_ID",
    "VBIO_ASSIGNED_GPU_ID",
    "CUDA_VISIBLE_DEVICES",
    "CELERY_WORKER_NAME",
    "CELERY_WORKER_HOSTNAME",
    "HOSTNAME",
    "VBIO_API_URL",
    "VITE_API_TOKEN",  # 见下：构建期由 [frontend] 的同名键注入
}

SCAN_ROOTS = [REPO_ROOT / "backend", REPO_ROOT / "frontend" / "server"]


def _example_keys() -> set[str]:
    with open(EXAMPLE, "rb") as handle:
        data = tomllib.load(handle)
    keys: set[str] = set()
    for section in data.values():
        if isinstance(section, dict):
            keys.update(section)
    return keys


def _code_keys() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for root in SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for match in KEY_RE.finditer(text):
                found.setdefault(match.group(1), []).append(
                    str(path.relative_to(REPO_ROOT)))
    return found


def test_every_code_referenced_key_is_documented():
    keys = _code_keys()
    documented = _example_keys()
    missing = sorted(
        key for key in keys
        if key not in documented and key not in RUNTIME_INJECTED
    )
    assert not missing, (
        "代码读取了模板未登记的配置键（加进 vbio.example.toml 对应小节，"
        f"或确认属于运行期注入并加入排除清单）：{missing}"
    )


def test_example_file_has_no_placeholder_sections():
    with open(EXAMPLE, "rb") as handle:
        data = tomllib.load(handle)
    assert isinstance(data, dict) and len(data) >= 8
    for section, entries in data.items():
        assert isinstance(entries, dict) and entries, f"[{section}] 为空小节"


def test_loader_round_trip_and_precedence(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys

    config = tmp_path / "vbio.toml"
    config.write_text(
        '[core]\nVBIO_API_TOKEN = "tok-1"\nMSA_CACHE_RETENTION_DAYS = 60\n'
        '[mail]\nSMTP_SSL = false\n',
        encoding="utf-8",
    )
    loader = REPO_ROOT / "configs" / "load_vbio_config.py"
    result = subprocess.run(
        [sys.executable, str(loader), "--file", str(config), "--export"],
        capture_output=True, text=True, check=True, env={k: v for k, v in os.environ.items() if k != "VBIO_API_TOKEN"},
    )
    assert "export VBIO_API_TOKEN='tok-1'" in result.stdout
    assert "export MSA_CACHE_RETENTION_DAYS='60'" in result.stdout
    assert "export SMTP_SSL='0'" in result.stdout

    env_file = tmp_path / "generated.env"
    subprocess.run(
        [sys.executable, str(loader), "--file", str(config),
         "--env-file", str(env_file)],
        capture_output=True, text=True, check=True,
    )
    body = env_file.read_text(encoding="utf-8")
    assert "VBIO_API_TOKEN=tok-1" in body
    assert "SMTP_SSL=0" in body

    # 真实环境优先：已存在的键不出现在 export 输出
    monkeypatch.setenv("VBIO_API_TOKEN", "live-overrides")
    result = subprocess.run(
        [sys.executable, str(loader), "--file", str(config), "--export"],
        capture_output=True, text=True, check=True,
    )
    assert "VBIO_API_TOKEN" not in result.stdout


def test_live_config_matches_template_if_present():
    live = REPO_ROOT / "configs" / "vbio.toml"
    if not live.exists():
        return
    with open(live, "rb") as handle:
        data = tomllib.load(handle)
    documented = _example_keys()
    unknown = sorted(
        key
        for section in data.values()
        if isinstance(section, dict)
        for key in section
        if key not in documented
    )
    assert not unknown, f"vbio.toml 存在模板外的键：{unknown}"
