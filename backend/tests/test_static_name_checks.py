from __future__ import annotations

import ast
import builtins
import symtable
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ROOTS = (
    "backend",
    "gpu_manager.py",
    "configs",
    "frontend/server",
    "frontend/server/tests",
    "capabilities/mol1",
    "capabilities/esmfold2",
    "capabilities/rfd4",
    "capabilities/protenix2dock",
    "capabilities/peptide_lm",
    "capabilities/boltz2score",
    "capabilities/halo",
    "capabilities/colabfold_server",
)
THIRD_PARTY = ("Mol-1-source", ".venv", "node_modules", "__pycache__")

_BUILTINS = set(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__package__", "__loader__",
    "__spec__", "__builtins__", "__cached__", "__path__",
}


def platform_sources():
    for root in ROOTS:
        path = REPO / root
        files = [path] if path.is_file() else sorted(path.rglob("*.py"))
        for source in files:
            if not any(part in THIRD_PARTY for part in source.parts):
                yield source


def undefined_global_refs(source: str) -> set[str]:
    table = symtable.symtable(source, "<module>", "exec")
    module_names = {symbol.get_name() for symbol in table.get_symbols()}
    undefined: set[str] = set()

    def walk(scope: symtable.SymbolTable) -> None:
        for child in scope.get_children():
            for symbol in child.get_symbols():
                name = symbol.get_name()
                if symbol.is_global() and not symbol.is_parameter() \
                        and name not in module_names and name not in _BUILTINS:
                    undefined.add(name)
            walk(child)

    walk(table)
    return undefined


class ScopeEvents(ast.NodeVisitor):
    def __init__(self, scope):
        self.reads: dict[str, list[tuple[int, int]]] = {}
        self.writes: dict[str, list[tuple[int, int]]] = {}
        self.escaped: set[str] = set()
        self.parameters = {arg.arg for arg in (
            *scope.args.posonlyargs, *scope.args.args, *scope.args.kwonlyargs)}
        for extra in (scope.args.vararg, scope.args.kwarg):
            if extra:
                self.parameters.add(extra.arg)

    def position(self, node):
        return (node.lineno, node.col_offset)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            self.reads.setdefault(node.id, []).append(self.position(node))
        elif isinstance(node.ctx, ast.Store):
            self.writes.setdefault(node.id, []).append(self.position(node))

    def visit_AugAssign(self, node):
        if isinstance(node.target, ast.Name):
            self.reads.setdefault(node.target.id, []).append(self.position(node))
        self.visit(node.value)

    def visit_AnnAssign(self, node):
        if isinstance(node.target, ast.Name) and node.value is not None:
            self.writes.setdefault(node.target.id, []).append(self.position(node.target))
        self.visit(node.annotation)
        if node.value is not None:
            self.visit(node.value)

    def visit_NamedExpr(self, node):
        self.visit(node.value)
        self.visit(node.target)

    def bind_aliases(self, node):
        for alias in node.names:
            bound = alias.asname or alias.name.split(".")[0]
            self.writes.setdefault(bound, []).append(self.position(node))

    def visit_Import(self, node):
        self.bind_aliases(node)

    def visit_ImportFrom(self, node):
        self.bind_aliases(node)

    def visit_Global(self, node):
        self.escaped.update(node.names)

    def visit_Nonlocal(self, node):
        self.escaped.update(node.names)

    def visit_ExceptHandler(self, node):
        if node.name:
            self.writes.setdefault(node.name, []).append(self.position(node))

    def visit_MatchAs(self, node):
        if node.name:
            self.writes.setdefault(node.name, []).append(self.position(node))

    def visit_MatchStar(self, node):
        if node.name:
            self.writes.setdefault(node.name, []).append(self.position(node))

    def visit_MatchMapping(self, node):
        if node.rest:
            self.writes.setdefault(node.rest, []).append(self.position(node))

    def visit_defaults(self, node):
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)

    def visit_FunctionDef(self, node):
        self.visit_defaults(node)
        for decorator in node.decorator_list:
            self.visit(decorator)

    def visit_AsyncFunctionDef(self, node):
        self.visit_defaults(node)
        for decorator in node.decorator_list:
            self.visit(decorator)

    def visit_Lambda(self, node):
        self.visit_defaults(node)

    def visit_ClassDef(self, node):
        self.writes.setdefault(node.name, []).append(self.position(node))
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)

    def visit_comprehension(self, node):
        self.visit(node.generators[0].iter)

    visit_ListComp = visit_comprehension
    visit_SetComp = visit_comprehension
    visit_DictComp = visit_comprehension
    visit_GeneratorExp = visit_comprehension


def locals_read_before_assignment(source: str) -> dict[str, int]:
    found: dict[str, int] = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            events = ScopeEvents(node)
            for statement in node.body:
                events.visit(statement)
            for name, writes in events.writes.items():
                reads = events.reads.get(name) or []
                if name in events.parameters or name in events.escaped or not reads:
                    continue
                if min(reads) < min(writes):
                    found.setdefault(name, min(reads)[0])
    return found


@pytest.mark.parametrize(
    "source", [str(path) for path in platform_sources()],
    ids=lambda path: str(Path(path).relative_to(REPO)),
)
def test_every_referenced_name_is_defined(source: str) -> None:
    text = Path(source).read_text(encoding="utf-8")
    assert not undefined_global_refs(text), f"未定义的全局名: {sorted(undefined_global_refs(text))}"
    assert not locals_read_before_assignment(text), (
        f"先读取后赋值的局部变量: {locals_read_before_assignment(text)}")


def test_checker_catches_a_local_read_before_assignment():
    source = "def f(items):\n    total = seen\n    seen = 1\n    return total\n"
    assert locals_read_before_assignment(source) == {"seen": 2}
    assert not undefined_global_refs(source)


def test_checker_catches_a_name_never_bound():
    source = "def f(row):\n    key = row['id']\n    return cache.pop(key)\n"
    assert undefined_global_refs(source) == {"cache"}


def test_checker_accepts_closure_variables_and_loops():
    source = (
        "def outer(rows):\n"
        "    cache = {}\n"
        "    def inner(row):\n"
        "        for key in row:\n"
        "            cache[key] = row[key]\n"
        "        return cache\n"
        "    return [inner(row) for row in rows]\n"
    )
    assert not undefined_global_refs(source)
    assert not locals_read_before_assignment(source)
