"""V-Bio 标准配置加载器。

configs/vbio.toml 是部署配置的单一事实源（模板见 vbio.example.toml）。
本工具把 TOML 中的键导出为环境变量：

  --export             输出 shell export 语句（供 run.sh eval，进程环境生效）
  --env-file <path>    写 docker env_file 格式文件（供 compose 注入容器）
  --check              对照模板校验：未知键 / 模板缺失键

优先级：真实环境变量 > vbio.toml > 代码默认值。导出时跳过调用环境中
已存在的键，保证现场覆盖（如临时改端口）不被配置文件回滚。
"""
from __future__ import annotations

import argparse
import os
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FILE = REPO_ROOT / "configs" / "vbio.toml"
EXAMPLE_FILE = REPO_ROOT / "configs" / "vbio.example.toml"


def load_pairs(path: Path) -> dict[str, str]:
    """读 TOML，展平为 {ENV 名: 字符串值}。布尔转 1/0，其余 str()。"""
    if not path.exists():
        raise SystemExit(f"配置文件不存在: {path}（模板见 vbio.example.toml）")
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    pairs: dict[str, str] = {}
    for section, entries in data.items():
        if not isinstance(entries, dict):
            raise SystemExit(f"[{section}] 小节必须是键值表")
        for key, value in entries.items():
            if not key.isupper() or not key.replace("_", "").isalnum():
                raise SystemExit(f"[{section}] {key} 不是合法环境变量名")
            if isinstance(value, bool):
                pairs[key] = "1" if value else "0"
            else:
                pairs[key] = str(value)
    return pairs


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def export_script(pairs: dict[str, str]) -> str:
    lines = []
    for key, value in sorted(pairs.items()):
        if key in os.environ:
            continue  # 现场环境优先
        lines.append(f"export {key}={shell_quote(value)}")
    return "\n".join(lines) + ("\n" if lines else "")


def env_file_text(pairs: dict[str, str]) -> str:
    lines = [f"{key}={value}" for key, value in sorted(pairs.items())]
    return "\n".join(lines) + ("\n" if lines else "")


def check(pairs: dict[str, str]) -> int:
    """对照模板：报告 vbio.toml 中的未知键（拼写错误或模板过期）。"""
    with open(EXAMPLE_FILE, "rb") as handle:
        known = {
            key
            for section in tomllib.load(handle).values()
            if isinstance(section, dict)
            for key in section
        }
    unknown = sorted(set(pairs) - known)
    if unknown:
        print(f"vbio.toml 存在模板外的键（检查拼写或升级模板）: {', '.join(unknown)}")
        return 1
    print(f"vbio.toml 校验通过（{len(pairs)} 键）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    parser.add_argument("--export", action="store_true", help="输出 export 语句")
    parser.add_argument("--env-file", type=Path, help="写 env_file 格式文件")
    parser.add_argument("--check", action="store_true", help="对照模板校验键名")
    args = parser.parse_args()

    pairs = load_pairs(args.file)
    if args.check:
        return check(pairs)
    if args.env_file:
        args.env_file.parent.mkdir(parents=True, exist_ok=True)
        args.env_file.write_text(env_file_text(pairs), encoding="utf-8")
        print(f"已生成 {args.env_file}（{len(pairs)} 键）")
    if args.export:
        sys.stdout.write(export_script(pairs))
    if not args.export and not args.env_file and not args.check:
        parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
