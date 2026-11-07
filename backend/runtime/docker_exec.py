"""Docker 容器流式执行与清理的共享实现（runtime 各 backend 复用）。

统一 `docker rm -f` 预清理与"stdout+stderr 合流写日志 + 回显 + 保留尾部"
的执行循环；各 backend 的失败诊断（hint/traceback 提取）留在调用侧。
"""
from __future__ import annotations

import subprocess
import sys
from typing import List, Tuple


def remove_container(name: Optional[str], timeout: int = 30) -> None:
    """`docker rm -f` 预清理同名残留容器；静默容错。"""
    if not name:
        return
    try:
        subprocess.run(
            ["docker", "rm", "-f", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
        )
    except Exception:
        pass


def run_docker_streaming(command: list, log_path: str, *,
                         echo: bool = True,
                         tail_lines: int = 200) -> Tuple[int, str]:
    """运行命令：stdout+stderr 合流写入 log_path 并回显 stderr，
    返回 (return_code, 尾部输出文本)。"""
    with open(log_path, "w", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        output_tail: List[str] = []
        if proc.stdout:
            for line in proc.stdout:
                log_file.write(line)
                log_file.flush()
                if echo:
                    print(line, end="", file=sys.stderr)
                output_tail.append(line)
                if len(output_tail) > tail_lines:
                    output_tail.pop(0)
        return_code = proc.wait()
    return return_code, "".join(output_tail[-tail_lines:])
