import builtins
import symtable
from pathlib import Path

import pytest

RUNTIME_DIR = Path(__file__).resolve().parents[1] / "runtime"

_BUILTINS = set(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__package__", "__loader__",
    "__spec__", "__builtins__", "__cached__",
}


def _undefined_refs(path: Path) -> list[tuple[str, str]]:
    table = symtable.symtable(path.read_text(encoding="utf-8"), str(path), "exec")
    module_names = {sym.get_name() for sym in table.get_symbols()}
    undefined: list[tuple[str, str]] = []

    def walk(scope: symtable.SymbolTable, prefix: str = "") -> None:
        for child in scope.get_children():
            label = f"{prefix}{child.get_name()}"
            for sym in child.get_symbols():
                if sym.is_global() and not sym.is_parameter():
                    name = sym.get_name()
                    if name not in module_names and name not in _BUILTINS:
                        undefined.append((label, name))
            walk(child, label + ".")

    walk(table)
    return undefined


@pytest.mark.parametrize(
    "module_path",
    sorted(str(p) for p in RUNTIME_DIR.rglob("*.py")),
    ids=lambda p: str(Path(p).relative_to(RUNTIME_DIR)),
)
def test_no_undefined_global_refs(module_path: str) -> None:
    refs = _undefined_refs(Path(module_path))
    assert not refs, f"未定义全局引用: {sorted({name for _scope, name in refs})}"
