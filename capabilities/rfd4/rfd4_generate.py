#!/usr/bin/env python3
"""RFD4-Proteina 生成引擎封装（V-Bio capability）。

在 rfd4-proteina-engine 镜像内运行，职责：
  条件化 YAML/JSON → rfd4-generate → 结构 cif + summary.json

GPU 规范：容器由宿主侧按单卡单任务派发（--runtime nvidia --gpus device=N），
封装内只做可用性断言与设备日志，不做多卡分发。
失败一律显式报错退出：生成非零退出、超时、产物缺失都直接抛错，不做兜底重试。
"""
from __future__ import annotations

import argparse
import glob
import json
import subprocess
import sys
import time
from pathlib import Path

AA3_TO_AA1 = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
}


def _log(msg: str) -> None:
    print(f"[rfd4] {msg}", file=sys.stderr, flush=True)


def _assert_gpu(allow_cpu: bool) -> None:
    import torch

    if torch.cuda.is_available():
        name = torch.cuda.get_device_name(0)
        total = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
        _log(f"GPU: {name}, {total:.0f} GiB")
        return
    if allow_cpu:
        _log("未检测到 CUDA 设备，按 --allow-cpu 以 CPU 诊断模式继续")
        return
    raise RuntimeError(
        "无可用 CUDA 设备。GPU 生成必须单卡派发"
        "（宿主侧 docker run --runtime nvidia --gpus device=N）；"
        "诊断可加 --allow-cpu。"
    )


def _resolve_spec(spec: Path, out_dir: Path) -> Path:
    """把条件化 spec 里的相对 input 路径解析为绝对路径，写副本返回。

    Hydra 运行时会切换 cwd，spec 里的相对 input 会失效；统一解析为绝对
    路径后免疫 cwd 变化。"""
    import yaml

    data = yaml.safe_load(spec.read_text())
    spec_dir = spec.resolve().parent
    for design in data.values():
        if not isinstance(design, dict):
            continue
        raw = design.get("input")
        if not raw:
            continue
        input_path = Path(raw)
        if input_path.is_absolute():
            design["input"] = str(input_path)
        elif input_path.exists():  # 相对 cwd（容器 /workspace）可直接命中
            design["input"] = str(input_path.resolve())
        else:  # 相对 spec 目录
            design["input"] = str((spec_dir / input_path).resolve())
    resolved = out_dir / (spec.stem + "_resolved" + spec.suffix)
    resolved.write_text(yaml.safe_dump(data, sort_keys=False))
    return resolved


def _build_overrides(args: argparse.Namespace, out_dir: Path) -> list[str]:
    overrides = [
        f"inference_experiment={args.preset}",
        f"inputs={args.resolved_spec}",
        f"out_dir={out_dir}",
        f"num_replicates={args.num_designs}",
        f"seed={args.seed}",
        "compile_lengths=null",
    ]
    for extra in args.extra or []:
        overrides.append(extra)
    return overrides


def _run_generation(overrides: list[str], timeout_min: float) -> None:
    cmd = ["rfd4-generate", *overrides]
    _log("执行: " + " ".join(cmd))
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, timeout=timeout_min * 60)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"rfd4-generate 超时（>{timeout_min:.0f} 分钟），已终止") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"rfd4-generate 退出码 {proc.returncode}")
    _log(f"生成完成，耗时 {time.monotonic() - started:.0f} 秒")


def _collect_cifs(out_dir: Path) -> list[Path]:
    cifs = sorted(out_dir.rglob("*.cif"))
    if not cifs:
        raise RuntimeError(f"{out_dir} 下没有任何结构产物")
    return cifs


def _chain_summary(cif_path: Path) -> dict:
    """按链提取残基数与 one-letter 序列；非标准残基记作 [CCD]。"""
    import gemmi

    st = gemmi.read_structure(str(cif_path))
    st.setup_entities()
    chains = {}
    for chain in st[0]:
        seq = []
        for res in chain:
            name = res.name.strip().upper()
            seq.append(AA3_TO_AA1.get(name, f"[{name}]"))
        chains[chain.name] = {
            "n_residues": len(seq),
            "sequence": "".join(seq),
        }
    return chains


def _write_summary(args: argparse.Namespace, out_dir: Path,
                   cifs: list[Path], elapsed_s: float) -> Path:
    summary = {
        "engine": "rfd4-proteina",
        "spec": str(Path(args.spec).resolve()),
        "preset": args.preset,
        "num_designs": args.num_designs,
        "seed": args.seed,
        "elapsed_seconds": round(elapsed_s, 1),
        "outputs": [],
    }
    for cif in cifs:
        entry = {"cif": str(cif)}
        try:
            entry["chains"] = _chain_summary(cif)
        except Exception as exc:  # 结构损坏时保留路径，链摘要置空
            entry["chains_error"] = str(exc)
        summary["outputs"].append(entry)
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    return summary_path


def main() -> None:
    ap = argparse.ArgumentParser(
        description="RFD4-Proteina 生成引擎封装：条件化 YAML 驱动，结构产物校验")
    ap.add_argument("--spec", required=True,
                    help="条件化 YAML/JSON（相对容器工作目录 /workspace）")
    ap.add_argument("--out-dir", required=True, help="输出目录")
    ap.add_argument("--num-designs", type=int, default=2, help="重复设计数")
    ap.add_argument("--seed", type=int, default=0, help="随机种子（决定复现性）")
    ap.add_argument("--preset", default="quickstart",
                    help="推理预设（quickstart 跳过评分与编译预热）")
    ap.add_argument("--timeout-min", type=float, default=120.0,
                    help="生成超时（分钟），超时显式报错")
    ap.add_argument("--allow-cpu", action="store_true",
                    help="允许无 GPU 运行（仅诊断用）")
    ap.add_argument("--extra", action="append", default=[],
                    help="追加 rfd4-generate 的 Hydra override，可多次")
    args = ap.parse_args()

    _assert_gpu(args.allow_cpu)

    spec = Path(args.spec)
    if not spec.is_file():
        raise SystemExit(f"条件化文件不存在: {spec}")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    import yaml  # noqa: F401  # 提前失败：spec 解析需要 yaml

    args.resolved_spec = _resolve_spec(spec, out_dir)
    overrides = _build_overrides(args, out_dir)
    started = time.monotonic()
    _run_generation(overrides, args.timeout_min)
    cifs = _collect_cifs(out_dir)
    summary_path = _write_summary(args, out_dir, cifs,
                                  time.monotonic() - started)
    print(json.dumps({"ok": True, "n_structures": len(cifs),
                      "summary": str(summary_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
