from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import time
from bisect import bisect_left
from functools import lru_cache
from itertools import permutations
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

try:
    import psycopg
    from psycopg.rows import dict_row as pg_dict_row
except Exception:
    psycopg = None
    pg_dict_row = None


logger = logging.getLogger(__name__)

_PG_DSN_PREFIXES = ("postgres://", "postgresql://")
_PG_SCHEMA_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class _DbCursor:
    def __init__(self, backend: str, cursor: Any):
        self.backend = backend
        self.cursor = cursor

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        statement = str(sql or "")
        if self.backend == "postgres":
            statement = statement.replace("?", "%s")
        if params is None:
            return self.cursor.execute(statement)
        return self.cursor.execute(statement, list(params))

    def fetchall(self) -> List[Any]:
        return self.cursor.fetchall()

    def fetchone(self) -> Any:
        return self.cursor.fetchone()


class _DbConnection:
    def __init__(self, backend: str, raw: Any):
        self.backend = backend
        self.raw = raw

    def cursor(self) -> _DbCursor:
        return _DbCursor(self.backend, self.raw.cursor())

    def close(self) -> None:
        try:
            self.raw.close()
        except Exception:
            pass


def _row_first(row: Any) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        if not row:
            return None
        return next(iter(row.values()))
    try:
        return row[0]
    except Exception:
        return None


def _row_value(row: Any, key: str, fallback_index: int = 0) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except Exception:
        pass
    try:
        return row[fallback_index]
    except Exception:
        return None


def _is_postgres_dsn(candidate: str) -> bool:
    token = str(candidate or "").strip().lower()
    return token.startswith(_PG_DSN_PREFIXES)


def _redact_postgres_dsn(dsn: str) -> str:
    token = str(dsn or "").strip()
    if not token:
        return token
    try:
        parsed = urlparse(token)
    except Exception:
        return "***"
    if not parsed.netloc:
        return "***"
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    database = parsed.path.lstrip("/") or "<db>"
    return f"{parsed.scheme}://***@{host}:{port}/{database}"


def _resolve_pg_schema(dsn: str) -> str:
    schema = "public"
    try:
        parsed = urlparse(str(dsn or ""))
        query = parse_qs(parsed.query or "")
        for key in ("schema", "search_path", "currentSchema"):
            values = query.get(key)
            if not values:
                continue
            value = str(values[0] or "").strip()
            if value:
                schema = value.split(",")[0].strip()
                break
    except Exception:
        schema = "public"
    if schema == "public":
        env_schema = str(os.getenv("LEAD_OPT_MMP_DB_SCHEMA", "") or "").strip()
        if env_schema:
            schema = env_schema
    if not _PG_SCHEMA_TOKEN_RE.match(schema):
        return "public"
    return schema


def _dsn_with_schema(dsn: str, schema: str) -> str:
    token = str(dsn or "").strip()
    if not token:
        return token
    try:
        parsed = urlparse(token)
        query = dict(parse_qsl(parsed.query or "", keep_blank_values=True))
        # psycopg/libpq doesn't accept JDBC-style schema URI params like currentSchema.
        # Keep runtime DSN clean; schema is applied via SET search_path after connect.
        query.pop("currentSchema", None)
        query.pop("schema", None)
        query.pop("search_path", None)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query), parsed.fragment))
    except Exception:
        return token


def _resolve_runtime_database(payload: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    payload_runtime = str(
        payload.get("mmp_database_runtime")
        or payload.get("runtime_database")
        or payload.get("mmp_database_url")
        or payload.get("database_url")
        or ""
    ).strip()
    payload_schema = str(payload.get("mmp_database_schema") or payload.get("schema") or "").strip()
    if _is_postgres_dsn(payload_runtime):
        runtime_database = _dsn_with_schema(payload_runtime, payload_schema or _resolve_pg_schema(payload_runtime))
        schema = payload_schema if _PG_SCHEMA_TOKEN_RE.match(payload_schema) else _resolve_pg_schema(payload_runtime)
        return runtime_database, {
            "id": str(payload.get("mmp_database_id") or "").strip(),
            "label": str(payload.get("mmp_database_label") or schema).strip() or schema,
            "schema": schema,
            "backend": "postgres",
            "source": "payload",
        }

    raise ValueError(
        "'mmp_database_runtime' is required and must be a PostgreSQL DSN. "
        "Please select a visible MMP database so API can inject runtime DSN."
    )


def _safe_json_object(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _normalize_atom_indices(value: Any) -> List[int]:
    if not isinstance(value, list):
        return []
    result: List[int] = []
    seen: set[int] = set()
    for item in value:
        try:
            idx = int(item)
        except Exception:
            continue
        if idx < 0 or idx in seen:
            continue
        seen.add(idx)
        result.append(idx)
    result.sort()
    return result


def _open_db_connection(database_path: str, schema_override: str = "") -> _DbConnection:
    if not _is_postgres_dsn(database_path):
        raise RuntimeError("Lead Optimization MMP query requires PostgreSQL DSN (runtime_database).")
    if psycopg is None:
        raise RuntimeError(
            "LEAD_OPT_MMP_DB_URL is set but psycopg is not installed. Install dependency: pip install psycopg[binary]"
        )
    conn = psycopg.connect(database_path, row_factory=pg_dict_row, autocommit=True)
    schema = str(schema_override or "").strip()
    if not _PG_SCHEMA_TOKEN_RE.match(schema):
        schema = _resolve_pg_schema(database_path)
    if schema:
        with conn.cursor() as cursor:
            if schema == "public":
                cursor.execute("SET search_path TO public")
            else:
                cursor.execute(f'SET search_path TO "{schema}", public')
    return _DbConnection("postgres", conn)


def normalize_variable_spec(variable_spec: Any) -> Dict[str, Any]:
    spec = _safe_json_object(variable_spec)
    if not spec:
        if isinstance(variable_spec, str):
            text = variable_spec.strip()
            if text:
                return {"items": [{"query": text, "mode": "substructure", "atom_indices": []}]}
        if isinstance(variable_spec, list):
            items = []
            for item in variable_spec:
                if isinstance(item, str) and item.strip():
                    items.append({"query": item.strip(), "mode": "substructure", "atom_indices": []})
                elif isinstance(item, dict):
                    query = str(item.get("query") or item.get("smarts") or item.get("smiles") or "").strip()
                    if query:
                        items.append(
                            {
                                "query": query,
                                "mode": str(item.get("mode") or "substructure").strip().lower(),
                                "fragment_id": str(item.get("fragment_id") or "").strip(),
                                "atom_indices": _normalize_atom_indices(item.get("atom_indices")),
                            }
                        )
            if items:
                return {"items": items}
        return {"items": []}

    mode = str(spec.get("mode") or "substructure").strip().lower()
    items = spec.get("items")
    if isinstance(items, list):
        normalized_items = []
        for item in items:
            if isinstance(item, str) and item.strip():
                normalized_items.append({"query": item.strip(), "mode": mode})
                continue
            if not isinstance(item, dict):
                continue
            query = str(item.get("query") or item.get("smarts") or item.get("smiles") or "").strip()
            atom_indices = _normalize_atom_indices(item.get("atom_indices"))
            if not query and not atom_indices:
                continue
            normalized_items.append(
                {
                    "query": query,
                    "mode": str(item.get("mode") or mode).strip().lower() or "substructure",
                    "fragment_id": str(item.get("fragment_id") or "").strip(),
                    "atom_indices": atom_indices,
                }
            )
        return {"items": normalized_items}

    query = str(spec.get("query") or spec.get("smarts") or spec.get("smiles") or "").strip()
    atom_indices = _normalize_atom_indices(spec.get("atom_indices"))
    if query or atom_indices:
        return {
            "items": [
                {
                    "query": query,
                    "mode": mode,
                    "fragment_id": str(spec.get("fragment_id") or "").strip(),
                    "atom_indices": atom_indices,
                }
            ]
        }
    return {"items": []}


def _normalize_smiles_like(value: str) -> str:
    try:
        from rdkit import Chem
    except Exception:
        return str(value or "").strip()
    text = str(value or "").strip()
    if not text:
        return ""
    mol = Chem.MolFromSmiles(text)
    if mol is None:
        return ""
    try:
        return Chem.MolToSmiles(mol, canonical=True)
    except Exception:
        return ""


def _normalize_attachment_query(query: str) -> str:
    try:
        from rdkit import Chem
    except Exception:
        return str(query or "").strip()
    text = str(query or "").strip()
    if not text:
        return ""
    # Prefer SMILES parser first to keep canonicalization consistent with mmpdb rule_smiles entries.
    mol = Chem.MolFromSmiles(text)
    if mol is None:
        mol = Chem.MolFromSmarts(text)
    if mol is None:
        return ""
    dummy_atoms = [atom for atom in mol.GetAtoms() if atom.GetAtomicNum() == 0]
    if not dummy_atoms:
        return ""
    # Canonicalize dummy labels to [*:1], [*:2], ... for stable DB matching.
    for idx, atom in enumerate(sorted(dummy_atoms, key=lambda item: int(item.GetIdx())), start=1):
        atom.SetAtomMapNum(idx)
        atom.SetIsotope(0)
    try:
        normalized = Chem.MolToSmiles(mol, canonical=True)
    except Exception:
        return ""
    if not normalized or "*" not in normalized:
        return ""
    # Keep valid attachment SMARTS/SMILES and avoid dropping multi-attachment aromatic fragments.
    return normalized


def _attachment_queries_equivalent(left: str, right: str) -> bool:
    try:
        from rdkit import Chem
    except Exception:
        return str(left or "").strip() == str(right or "").strip()
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    if not left_text or not right_text:
        return False
    left_mol = Chem.MolFromSmiles(left_text) or Chem.MolFromSmarts(left_text)
    right_mol = Chem.MolFromSmiles(right_text) or Chem.MolFromSmarts(right_text)
    if left_mol is None or right_mol is None:
        return False
    for mol in (left_mol, right_mol):
        for atom in mol.GetAtoms():
            if int(atom.GetAtomicNum()) == 0:
                atom.SetAtomMapNum(0)
                atom.SetIsotope(0)
    return bool(left_mol.HasSubstructMatch(right_mol) and right_mol.HasSubstructMatch(left_mol))


def _expand_atom_indices_to_complete_rings(parent: Any, atom_indices: set[int]) -> set[int]:
    expanded = set(int(idx) for idx in atom_indices)
    if not expanded:
        return expanded
    try:
        ring_info = parent.GetRingInfo()
        rings = [set(int(i) for i in ring) for ring in ring_info.AtomRings()]
    except Exception:
        rings = []

    changed = True
    while changed:
        changed = False
        for ring in rings:
            if expanded.intersection(ring) and not ring.issubset(expanded):
                expanded.update(ring)
                changed = True

    # Matcher-style behavior: if explicit H touches selected variable atoms, keep it in the variable side.
    extra_h: set[int] = set()
    for atom_idx in list(expanded):
        try:
            atom = parent.GetAtomWithIdx(atom_idx)
        except Exception:
            continue
        for neighbor in atom.GetNeighbors():
            if int(neighbor.GetAtomicNum()) == 1:
                extra_h.add(int(neighbor.GetIdx()))
    expanded.update(extra_h)
    return expanded


def derive_attachment_query_from_atom_indices(query_mol: Any, atom_indices: List[int], *, expand_rings: bool = True) -> str:
    try:
        from rdkit import Chem
    except Exception:
        return ""
    parent = None
    if query_mol is not None and hasattr(query_mol, "GetNumAtoms"):
        parent = query_mol
    else:
        query_text = str(query_mol or "").strip()
        parent = Chem.MolFromSmiles(query_text)
        if parent is None and query_text:
            # Keep atom order when possible for index-driven selections, even for partially non-kekulized inputs.
            parent = Chem.MolFromSmiles(query_text, sanitize=False)
            if parent is not None:
                try:
                    Chem.SanitizeMol(
                        parent,
                        sanitizeOps=Chem.SanitizeFlags.SANITIZE_ALL ^ Chem.SanitizeFlags.SANITIZE_KEKULIZE,
                    )
                except Exception:
                    pass
    if parent is None:
        return ""
    atom_set = {
        int(idx)
        for idx in atom_indices
        if isinstance(idx, int) and 0 <= int(idx) < parent.GetNumAtoms()
    }
    if expand_rings:
        atom_set = _expand_atom_indices_to_complete_rings(parent, atom_set)
    if not atom_set:
        return ""

    boundary_bond_indices: set[int] = set()
    for atom_idx in atom_set:
        atom = parent.GetAtomWithIdx(atom_idx)
        for neighbor in atom.GetNeighbors():
            neighbor_idx = int(neighbor.GetIdx())
            if neighbor_idx in atom_set:
                continue
            bond = parent.GetBondBetweenAtoms(atom_idx, neighbor_idx)
            if bond is not None:
                boundary_bond_indices.add(int(bond.GetIdx()))
    if not boundary_bond_indices:
        return ""

    fragmented = Chem.FragmentOnBonds(parent, sorted(boundary_bond_indices), addDummies=True)
    fragments = Chem.GetMolFrags(fragmented, asMols=False, sanitizeFrags=False)
    selected_atoms: Optional[Tuple[int, ...]] = None
    best_overlap = -1
    for frag_atoms in fragments:
        overlap = len(atom_set.intersection(int(a) for a in frag_atoms))
        if overlap > best_overlap:
            best_overlap = overlap
            selected_atoms = tuple(int(a) for a in frag_atoms)
    if not selected_atoms or best_overlap <= 0:
        return ""
    try:
        query = Chem.MolFragmentToSmiles(fragmented, atomsToUse=list(selected_atoms), canonical=True)
    except Exception:
        return ""
    normalized = _normalize_attachment_query(query)
    if not normalized or "*" not in normalized:
        return ""
    return normalized


def derive_attachment_queries_from_context(query_mol: str, variable_query: str) -> List[str]:
    try:
        from rdkit import Chem
    except Exception:
        return []

    parent = Chem.MolFromSmiles(str(query_mol or "").strip())
    if parent is None:
        return []
    query_text = str(variable_query or "").strip()
    if not query_text:
        return []
    pattern = Chem.MolFromSmarts(query_text) or Chem.MolFromSmiles(query_text)
    if pattern is None:
        return []

    results: set[str] = set()
    matches = parent.GetSubstructMatches(pattern, uniquify=True)
    for match in matches[:96]:
        match_set = set(int(idx) for idx in match)
        match_set = _expand_atom_indices_to_complete_rings(parent, match_set)
        if not match_set:
            continue

        boundary_bond_indices: set[int] = set()
        for atom_idx in match_set:
            atom = parent.GetAtomWithIdx(atom_idx)
            for neighbor in atom.GetNeighbors():
                neighbor_idx = int(neighbor.GetIdx())
                if neighbor_idx in match_set:
                    continue
                bond = parent.GetBondBetweenAtoms(atom_idx, neighbor_idx)
                if bond is not None:
                    boundary_bond_indices.add(int(bond.GetIdx()))
        if not boundary_bond_indices:
            continue

        fragmented = Chem.FragmentOnBonds(parent, sorted(boundary_bond_indices), addDummies=True)
        fragments = Chem.GetMolFrags(fragmented, asMols=False, sanitizeFrags=False)
        selected_atoms: Optional[Tuple[int, ...]] = None
        best_overlap = -1
        for frag_atoms in fragments:
            overlap = len(match_set.intersection(int(a) for a in frag_atoms))
            if overlap > best_overlap:
                best_overlap = overlap
                selected_atoms = tuple(int(a) for a in frag_atoms)
        if not selected_atoms or best_overlap <= 0:
            continue

        try:
            frag_smiles = Chem.MolFragmentToSmiles(fragmented, atomsToUse=list(selected_atoms), canonical=True)
        except Exception:
            continue
        normalized = _normalize_attachment_query(frag_smiles)
        if normalized and "*" in normalized:
            results.add(normalized)
    return sorted(results)


def build_mmp_query_list_from_variable_spec(variable_spec: Dict[str, Any], query_mol: str = "") -> List[str]:
    queries: List[str] = []
    seen: set[str] = set()
    rejected_selection_count = 0
    for item in variable_spec.get("items", []):
        query = str(item.get("query") or "").strip()
        fragment_id = str(item.get("fragment_id") or "").strip()
        atom_indices = _normalize_atom_indices(item.get("atom_indices"))
        if not query and not atom_indices:
            continue
        candidates: List[str] = []
        if atom_indices and query_mol:
            direct = derive_attachment_query_from_atom_indices(query_mol, atom_indices, expand_rings=False)
            if direct:
                normalized_expected = _normalize_attachment_query(query) if "*" in query else ""
                if normalized_expected and max(1, normalized_expected.count("*")) != max(1, direct.count("*")):
                    rejected_selection_count += 1
                    logger.warning(
                        "Lead-opt selected fragment '%s' attachment mismatch: expected=%s derived=%s query='%s' derived='%s'",
                        fragment_id or query,
                        normalized_expected.count("*"),
                        direct.count("*"),
                        normalized_expected,
                        direct,
                    )
                    continue
                if normalized_expected and not _attachment_queries_equivalent(normalized_expected, direct):
                    rejected_selection_count += 1
                    logger.warning(
                        "Lead-opt selected fragment '%s' structure mismatch: expected='%s' derived='%s'",
                        fragment_id or query,
                        normalized_expected,
                        direct,
                    )
                    continue
                candidates.append(direct)
            else:
                rejected_selection_count += 1
                logger.warning(
                    "Lead-opt selected fragment '%s' could not be converted to an attachment-aware query from atom indices.",
                    fragment_id or query,
                )
                # Selection-driven items must stay atom-index deterministic; do not fallback to text heuristics.
                continue
        else:
            if "*" in query:
                normalized = _normalize_attachment_query(query)
                if normalized:
                    candidates.append(normalized)
                else:
                    logger.info("Lead-opt variable query '%s' was rejected: invalid attachment fragment.", query)
            if not candidates:
                candidates = derive_attachment_queries_from_context(query_mol, query) if query_mol else []
                if not candidates:
                    logger.info(
                        "Lead-opt variable query '%s' has no attachment marker and could not be attachment-expanded.",
                        query,
                    )
                    continue
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            queries.append(candidate)
    if rejected_selection_count > 0 and not queries:
        logger.warning(
            "All selected fragments were rejected for MMP query because no attachment-aware cut could be derived."
        )
    return queries


def _compute_descriptor_value(smiles: str, property_name: str) -> Optional[float]:
    token = str(property_name or "").strip().lower()
    if token not in {"mw", "logp", "tpsa"}:
        return None
    try:
        from rdkit import Chem
        from rdkit.Chem import Crippen, Descriptors, rdMolDescriptors
    except Exception:
        return None
    mol = Chem.MolFromSmiles(str(smiles or "").strip())
    if mol is None:
        return None
    try:
        if token == "mw":
            return float(Descriptors.MolWt(mol))
        if token == "logp":
            return float(Crippen.MolLogP(mol))
        if token == "tpsa":
            return float(rdMolDescriptors.CalcTPSA(mol))
    except Exception:
        return None
    return None


def _list_available_db_properties(database_path: str, schema_override: str = "") -> List[str]:
    if not database_path:
        return []
    if not _is_postgres_dsn(database_path):
        return []
    try:
        conn = _open_db_connection(database_path, schema_override=schema_override)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM property_name ORDER BY name")
            rows = cursor.fetchall()
            values = [str(_row_first(row) or "").strip() for row in rows]
            return [value for value in values if value]
        finally:
            conn.close()
    except Exception:
        return []


def _resolve_property_for_db(requested: str, database_path: str, schema_override: str = "") -> str:
    token = str(requested or "").strip().lower()
    if not token:
        return ""
    available = _list_available_db_properties(database_path, schema_override=schema_override)
    if not available:
        return ""
    by_lower = {name.lower(): name for name in available}
    aliases = {
        "potency": ["potency", "pic50", "pchembl", "activity"],
        "mw": ["mw", "molecular_weight", "mol_weight", "molwt"],
        "logp": ["logp", "alogp", "xlogp", "clogp"],
        "tpsa": ["tpsa", "psa"],
    }
    for candidate in aliases.get(token, [token]):
        resolved = by_lower.get(candidate.lower())
        if resolved:
            return resolved
    return ""


@lru_cache(maxsize=128)
def _attachment_permutations(fragment_smiles: str) -> Tuple[str, ...]:
    text = str(fragment_smiles or "").strip()
    if not text:
        return tuple()
    wildcard_pattern = re.compile(r"\[\*:\d+\]|\*")
    matches = list(wildcard_pattern.finditer(text))
    normalized = _normalize_attachment_query(text)
    if not matches:
        if normalized:
            return (normalized,)
        return (text,)

    if len(matches) > 3:
        if normalized:
            return (normalized,)
        return (text,)

    template = text
    for index, match in reversed(list(enumerate(matches))):
        start, end = match.span()
        placeholder = f"__W{index}__"
        template = template[:start] + placeholder + template[end:]

    outputs: set[str] = set()
    labels = list(range(1, len(matches) + 1))
    for label_order in permutations(labels):
        candidate = template
        for idx, label in enumerate(label_order):
            candidate = candidate.replace(f"__W{idx}__", f"[*:{int(label)}]")
        normalized_candidate = _normalize_attachment_query(candidate)
        if normalized_candidate:
            outputs.add(normalized_candidate)
    if normalized:
        outputs.add(normalized)
    if outputs:
        return tuple(sorted(outputs))
    return (text,)


def _chunked(values: Sequence[int], size: int = 500) -> Iterable[Sequence[int]]:
    chunk_size = max(1, int(size))
    for start in range(0, len(values), chunk_size):
        yield values[start : start + chunk_size]


def _attachment_search_tokens(fragment: str) -> List[str]:
    text = str(fragment or "").strip()
    if not text:
        return []
    stripped = re.sub(r"\[\*:\d+\]|\*", ".", text)
    pieces = [p.strip(".") for p in stripped.split(".") if p.strip(".")]
    regex_tokens = re.findall(r"[A-Za-z0-9]{4,}", stripped)
    tokens = [piece for piece in pieces if len(piece) >= 4 and any(ch.isalpha() for ch in piece)]
    tokens.extend(regex_tokens)
    dedup: List[str] = []
    seen: set[str] = set()
    for token in sorted(tokens, key=len, reverse=True):
        if token in seen:
            continue
        seen.add(token)
        dedup.append(token)
    return dedup[:6]


def _resolve_variable_mode(variable_spec: Dict[str, Any]) -> str:
    modes = {
        str((item or {}).get("mode") or "").strip().lower()
        for item in (variable_spec.get("items") if isinstance(variable_spec.get("items"), list) else [])
    }
    modes.discard("")
    if "substructure" in modes:
        return "substructure"
    if "exact" in modes:
        return "exact"
    return "substructure"


def _table_has_column(conn: _DbConnection, table_name: str, column_name: str) -> bool:
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = ?
              AND column_name = ?
            LIMIT 1
            """,
            (str(table_name), str(column_name)),
        )
        row = cursor.fetchone()
        return row is not None
    except Exception:
        return False


def _lookup_rule_smiles_ids_postgres_substructure(
    conn: _DbConnection,
    *,
    forms: List[str],
    has_num_frags: bool,
    min_heavy: int,
    max_heavy: int,
    max_rule_smiles: int,
) -> List[int]:
    if not forms:
        return []
    cursor = conn.cursor()
    ids: set[int] = set()
    for fragment in forms:
        token = str(fragment or "").strip()
        if not token:
            continue
        num_frags = max(1, token.count("*"))
        where_clauses = ["smiles_mol @> mol_from_smiles(?)"]
        params: List[Any] = [token]
        if has_num_frags:
            # Keep matcher semantics: variable fragment and DB fragment must share the same attachment count.
            where_clauses.append("num_frags = ?")
            params.append(num_frags)
        if max_heavy > 0:
            where_clauses.append("num_heavies BETWEEN ? AND ?")
            params.extend([max(0, int(min_heavy)), max(0, int(max_heavy))])
        params.append(int(max_rule_smiles))
        sql = f"SELECT id FROM rule_smiles WHERE {' AND '.join(where_clauses)} LIMIT ?"
        try:
            cursor.execute(sql, params)
        except Exception:
            logger.debug("Lead-opt MMP substructure SQL skipped for fragment=%s", token, exc_info=True)
            continue
        for row in cursor.fetchall():
            value = _row_first(row)
            if value is None:
                continue
            ids.add(int(value))
            if len(ids) >= max_rule_smiles:
                return sorted(ids)[:max_rule_smiles]
    return sorted(ids)[:max_rule_smiles]


def _lookup_rule_smiles_ids(
    conn: _DbConnection,
    *,
    variable_queries: List[str],
    variable_mode: str,
    max_rule_smiles: int,
) -> List[int]:
    cursor = conn.cursor()
    exact_forms: set[str] = set()
    for fragment in variable_queries:
        for permutation_smiles in _attachment_permutations(fragment):
            if permutation_smiles:
                exact_forms.add(permutation_smiles)
    if not exact_forms:
        return []
    attachment_counts = [max(1, str(fragment).count("*")) for fragment in exact_forms]
    min_frags = min(attachment_counts) if attachment_counts else 1
    max_frags = max(attachment_counts) if attachment_counts else max(1, min_frags)
    min_heavy = 0
    max_heavy = 0
    try:
        from rdkit import Chem

        heavy_counts: List[int] = []
        for fragment in exact_forms:
            mol = Chem.MolFromSmiles(fragment) or Chem.MolFromSmarts(fragment)
            if mol is None:
                continue
            heavy_counts.append(max(0, int(mol.GetNumHeavyAtoms())))
        if heavy_counts:
            min_heavy = max(0, min(heavy_counts) - 2)
            # Keep substructure broad enough for medicinal chem variants, but avoid jumping to oversized contexts.
            max_heavy = max(0, max(heavy_counts) + max(4, min_frags * 2))
    except Exception:
        min_heavy = 0
        max_heavy = 0
    has_num_frags = _table_has_column(conn, "rule_smiles", "num_frags")
    has_smiles_mol = conn.backend == "postgres" and _table_has_column(conn, "rule_smiles", "smiles_mol")
    if has_num_frags:
        attachment_where = "AND num_frags BETWEEN ? AND ?"
    elif max_frags <= 1:
        attachment_where = "AND smiles NOT LIKE '%[*:2]%'"
    elif min_frags >= 3:
        attachment_where = "AND smiles LIKE '%[*:3]%'"
    elif min_frags >= 2 and max_frags <= 2:
        attachment_where = "AND smiles LIKE '%[*:2]%' AND smiles NOT LIKE '%[*:3]%'"
    else:
        attachment_where = ""

    ids: set[int] = set()
    form_list = sorted(exact_forms)
    for chunk in _chunked(form_list, size=400):
        placeholders = ",".join("?" for _ in chunk)
        if has_num_frags:
            sql = f"SELECT id FROM rule_smiles WHERE smiles IN ({placeholders}) AND num_frags BETWEEN ? AND ?"
            params: List[Any] = list(chunk) + [min_frags, max_frags]
        else:
            sql = f"SELECT id FROM rule_smiles WHERE smiles IN ({placeholders})"
            params = list(chunk)
        cursor.execute(sql, params)
        for row in cursor.fetchall():
            value = _row_first(row)
            if value is not None:
                ids.add(int(value))
        if len(ids) >= max_rule_smiles:
            break

    # Match matcher backend behavior on PostgreSQL: use cartridge substructure search over smiles_mol,
    # with exact attachment-count gating (num_frags).
    if variable_mode == "substructure" and has_smiles_mol:
        return _lookup_rule_smiles_ids_postgres_substructure(
            conn,
            forms=form_list,
            has_num_frags=has_num_frags,
            min_heavy=min_heavy,
            max_heavy=max_heavy,
            max_rule_smiles=max_rule_smiles,
        )

    def _normalize_dummy_labels(mol: Any) -> None:
        for atom in mol.GetAtoms():
            if int(atom.GetAtomicNum()) == 0:
                atom.SetAtomMapNum(0)
                atom.SetIsotope(0)

    def _exact_graph_match_ids(existing_ids: set[int]) -> set[int]:
        try:
            from rdkit import Chem
        except Exception:
            return existing_ids
        exact_patterns: List[Any] = []
        heavy_targets: set[int] = set()
        atom_targets: set[int] = set()
        for query in variable_queries:
            pattern = Chem.MolFromSmarts(query) or Chem.MolFromSmiles(query)
            if pattern is None:
                continue
            _normalize_dummy_labels(pattern)
            exact_patterns.append(pattern)
            heavy_targets.add(max(0, int(pattern.GetNumHeavyAtoms())))
            atom_targets.add(max(0, int(pattern.GetNumAtoms())))
        if not exact_patterns:
            return existing_ids

        min_heavy_target = min(heavy_targets) if heavy_targets else 0
        max_heavy_target = max(heavy_targets) if heavy_targets else 0
        min_atom_target = min(atom_targets) if atom_targets else 0
        max_atom_target = max(atom_targets) if atom_targets else 0
        min_heavy_bound = max(0, min_heavy_target - 1)
        max_heavy_bound = max_heavy_target + 1

        cursor_local = conn.cursor()
        if has_num_frags:
            sql = (
                "SELECT id, smiles FROM rule_smiles "
                "WHERE num_frags BETWEEN ? AND ? "
                "AND num_heavies BETWEEN ? AND ?"
            )
            params_local: List[Any] = [min_frags, max_frags, min_heavy_bound, max_heavy_bound]
        else:
            sql = "SELECT id, smiles FROM rule_smiles WHERE num_heavies BETWEEN ? AND ?"
            params_local = [min_heavy_bound, max_heavy_bound]
        cursor_local.execute(sql, params_local)
        for row in cursor_local.fetchall():
            try:
                rid = int(_row_value(row, "id", fallback_index=0))
            except Exception:
                continue
            if rid in existing_ids:
                continue
            smiles = str(_row_value(row, "smiles", fallback_index=1) or "")
            mol = Chem.MolFromSmiles(smiles)
            if mol is None:
                continue
            _normalize_dummy_labels(mol)
            atom_count = int(mol.GetNumAtoms())
            if atom_count < min_atom_target or atom_count > max_atom_target:
                continue
            heavy_count = int(mol.GetNumHeavyAtoms())
            if heavy_count < min_heavy_target or heavy_count > max_heavy_target:
                continue
            for pattern in exact_patterns:
                if int(pattern.GetNumAtoms()) != atom_count:
                    continue
                if int(pattern.GetNumHeavyAtoms()) != heavy_count:
                    continue
                if bool(mol.HasSubstructMatch(pattern) and pattern.HasSubstructMatch(mol)):
                    existing_ids.add(rid)
                    break
            if len(existing_ids) >= max_rule_smiles:
                break
        return existing_ids

    if variable_mode == "exact":
        ids = _exact_graph_match_ids(ids)
        return sorted(ids)[:max_rule_smiles]

    def _compile_substructure_patterns(queries: List[str]) -> List[Any]:
        try:
            from rdkit import Chem
        except Exception:
            return []
        compiled: List[Any] = []
        for query in queries:
            pattern = Chem.MolFromSmarts(query) or Chem.MolFromSmiles(query)
            if pattern is None:
                continue
            # Substructure mode ignores attachment label numbering.
            for atom in pattern.GetAtoms():
                if atom.GetAtomicNum() == 0:
                    atom.SetAtomMapNum(0)
                    atom.SetIsotope(0)
            compiled.append(pattern)
        return compiled

    def _filter_ids_by_patterns(source_ids: Iterable[int], patterns: List[Any], limit: int) -> List[int]:
        if not patterns:
            return sorted({int(i) for i in source_ids if int(i) > 0})[:limit]
        source = sorted({int(i) for i in source_ids if int(i) > 0})
        if not source:
            return []
        try:
            from rdkit import Chem
        except Exception:
            return source[:limit]
        filtered: List[int] = []
        for chunk in _chunked(source, size=500):
            placeholders = ",".join("?" for _ in chunk)
            cursor.execute(
                f"SELECT id, smiles FROM rule_smiles WHERE id IN ({placeholders})",
                list(chunk),
            )
            for row in cursor.fetchall():
                rid = int(row["id"])
                smiles = str(row["smiles"] or "")
                mol = Chem.MolFromSmiles(smiles)
                if mol is None:
                    continue
                if any(mol.HasSubstructMatch(pattern) for pattern in patterns):
                    filtered.append(rid)
                    if len(filtered) >= limit:
                        return sorted(set(filtered))[:limit]
        return sorted(set(filtered))[:limit]

    patterns = _compile_substructure_patterns(variable_queries)
    if not patterns:
        return sorted(ids)[:max_rule_smiles]
    ids = set(_filter_ids_by_patterns(ids, patterns, max_rule_smiles))
    if len(ids) >= max_rule_smiles:
        return sorted(ids)[:max_rule_smiles]

    # Substructure mode is an explicit query mode, not a fallback.
    search_tokens: List[str] = []
    for query in variable_queries:
        search_tokens.extend(_attachment_search_tokens(query))
    dedup_tokens = []
    seen_tokens: set[str] = set()
    for token in search_tokens:
        if token in seen_tokens:
            continue
        seen_tokens.add(token)
        dedup_tokens.append(token)
    for token in dedup_tokens[:6]:
        sql = f"""
            SELECT id
            FROM rule_smiles
            WHERE smiles LIKE ?
              {"AND num_heavies BETWEEN ? AND ?" if max_heavy > 0 else ""}
              {attachment_where}
            LIMIT ?
        """
        if has_num_frags:
            if max_heavy > 0:
                params = (
                    f"%{token}%",
                    min_heavy,
                    max_heavy,
                    min_frags,
                    max_frags,
                    max(2000, max_rule_smiles),
                )
            else:
                params = (f"%{token}%", min_frags, max_frags, max(2000, max_rule_smiles))
        else:
            if max_heavy > 0:
                params = (f"%{token}%", min_heavy, max_heavy, max(2000, max_rule_smiles))
            else:
                params = (f"%{token}%", max(2000, max_rule_smiles))
        cursor.execute(sql, params)
        for row in cursor.fetchall():
            value = _row_first(row)
            if value is not None:
                ids.add(int(value))
        if len(ids) >= max(2000, max_rule_smiles):
            break

    matched_ids = set(_filter_ids_by_patterns(ids, patterns, max_rule_smiles))
    if len(matched_ids) >= max_rule_smiles:
        return sorted(matched_ids)[:max_rule_smiles]

    if max_heavy <= 0:
        return sorted(matched_ids)[:max_rule_smiles]

    try:
        from rdkit import Chem
    except Exception:
        return _filter_ids_by_patterns(ids, patterns, max_rule_smiles)

    heavies = [max(0, int(p.GetNumHeavyAtoms())) for p in patterns]
    pattern_min_heavy = max(0, min(heavies) - 2)
    pattern_max_heavy = max(0, max(heavies) + max(4, min_frags * 2))
    min_heavy = max(min_heavy, pattern_min_heavy)
    max_heavy = min(max_heavy, pattern_max_heavy)
    if max_heavy <= 0 or max_heavy < min_heavy:
        max_heavy = max(pattern_max_heavy, min_heavy)
    page_size = 50000
    last_id = 0
    while len(matched_ids) < max_rule_smiles:
        sql = f"""
            SELECT id, smiles
            FROM rule_smiles
            WHERE id > ?
              AND num_heavies BETWEEN ? AND ?
              {attachment_where}
            ORDER BY id ASC
            LIMIT ?
        """
        if has_num_frags:
            params = (last_id, min_heavy, max_heavy, min_frags, max_frags, page_size)
        else:
            params = (last_id, min_heavy, max_heavy, page_size)
        cursor.execute(sql, params)
        batch = cursor.fetchall()
        if not batch:
            break
        for row in batch:
            rid = int(row["id"])
            smiles = str(row["smiles"] or "")
            mol = Chem.MolFromSmiles(smiles)
            if mol is None:
                continue
            matched = any(mol.HasSubstructMatch(pattern) for pattern in patterns)
            if not matched:
                continue
            matched_ids.add(rid)
            if len(matched_ids) >= max_rule_smiles:
                break
        last_id = int(batch[-1]["id"])
    return sorted(matched_ids)[:max_rule_smiles]


def _fetch_rule_environment_candidates(
    conn: _DbConnection,
    *,
    rule_smiles_ids: List[int],
    min_pairs: int,
    max_env_rows: int,
) -> List[Dict[str, Any]]:
    if not rule_smiles_ids:
        return []
    cursor = conn.cursor()
    has_num_frags = _table_has_column(conn, "rule_smiles", "num_frags")
    placeholders = ",".join("?" for _ in rule_smiles_ids)
    params: List[Any] = []
    params.extend(rule_smiles_ids)
    params.append(int(min_pairs))
    params.extend(rule_smiles_ids)
    params.append(int(min_pairs))
    params.append(int(max_env_rows))
    from_num_frags_col = "rs_from.num_frags AS from_num_frags," if has_num_frags else "0 AS from_num_frags,"
    to_num_frags_col = "rs_to.num_frags AS to_num_frags," if has_num_frags else "0 AS to_num_frags,"
    sql = f"""
    WITH matched AS (
        SELECT
            re.id AS rule_environment_id,
            re.rule_id AS rule_id,
            re.radius AS radius,
            re.num_pairs AS num_pairs,
            rs_from.smiles AS from_smiles,
            rs_to.smiles AS to_smiles,
            {from_num_frags_col}
            {to_num_frags_col}
            'forward' AS orientation
        FROM rule_environment re
        INNER JOIN rule r ON r.id = re.rule_id
        INNER JOIN rule_smiles rs_from ON rs_from.id = r.from_smiles_id
        INNER JOIN rule_smiles rs_to ON rs_to.id = r.to_smiles_id
        WHERE r.from_smiles_id IN ({placeholders})
          AND re.num_pairs >= ?

        UNION ALL

        SELECT
            re.id AS rule_environment_id,
            re.rule_id AS rule_id,
            re.radius AS radius,
            re.num_pairs AS num_pairs,
            rs_to.smiles AS from_smiles,
            rs_from.smiles AS to_smiles,
            {to_num_frags_col}
            {from_num_frags_col}
            'reverse' AS orientation
        FROM rule_environment re
        INNER JOIN rule r ON r.id = re.rule_id
        INNER JOIN rule_smiles rs_from ON rs_from.id = r.from_smiles_id
        INNER JOIN rule_smiles rs_to ON rs_to.id = r.to_smiles_id
        WHERE r.to_smiles_id IN ({placeholders})
          AND re.num_pairs >= ?
    )
    SELECT *
    FROM matched
    ORDER BY num_pairs DESC, rule_environment_id ASC
    LIMIT ?
    """
    cursor.execute(sql, params)
    rows = cursor.fetchall()
    return [
        {
            "rule_environment_id": int(row["rule_environment_id"]),
            "rule_id": int(row["rule_id"]),
            "rule_env_radius": int(row["radius"] or 0),
            "rule_env_pairs": int(row["num_pairs"] or 0),
            "from_smiles": str(row["from_smiles"] or ""),
            "to_smiles": str(row["to_smiles"] or ""),
            "from_num_frags": int(row["from_num_frags"] or 0) if int(row["from_num_frags"] or 0) > 0 else max(1, str(row["from_smiles"] or "").count("*")),
            "to_num_frags": int(row["to_num_frags"] or 0) if int(row["to_num_frags"] or 0) > 0 else max(1, str(row["to_smiles"] or "").count("*")),
            "orientation": str(row["orientation"] or "forward"),
        }
        for row in rows
    ]


def _fetch_pair_counts(
    conn: _DbConnection,
    *,
    rule_environment_ids: List[int],
) -> Dict[Tuple[int, int], int]:
    if not rule_environment_ids:
        return {}
    cursor = conn.cursor()
    counts: Dict[Tuple[int, int], int] = {}
    for chunk in _chunked(rule_environment_ids, size=300):
        placeholders = ",".join("?" for _ in chunk)
        sql = f"""
        SELECT rule_environment_id, COALESCE(constant_id, -1) AS constant_id, COUNT(*) AS pair_count
        FROM pair
        WHERE rule_environment_id IN ({placeholders})
        GROUP BY rule_environment_id, COALESCE(constant_id, -1)
        """
        cursor.execute(sql, list(chunk))
        for row in cursor.fetchall():
            key = (int(row["rule_environment_id"]), int(row["constant_id"]))
            counts[key] = int(row["pair_count"] or 0)
    return counts


def _fetch_pair_samples(
    conn: _DbConnection,
    *,
    rule_environment_ids: List[int],
    per_env_constant_limit: int,
) -> List[Dict[str, Any]]:
    if not rule_environment_ids:
        return []
    cursor = conn.cursor()
    rows: List[Dict[str, Any]] = []
    for chunk in _chunked(rule_environment_ids, size=120):
        placeholders = ",".join("?" for _ in chunk)
        sql = f"""
        WITH ranked_pairs AS (
            SELECT
                p.rule_environment_id AS rule_environment_id,
                COALESCE(p.constant_id, -1) AS constant_id,
                p.compound1_id AS compound1_id,
                p.compound2_id AS compound2_id,
                p.id AS pair_id,
                ROW_NUMBER() OVER (
                    PARTITION BY p.rule_environment_id, COALESCE(p.constant_id, -1)
                    ORDER BY p.id ASC
                ) AS rank_in_constant
            FROM pair p
            WHERE p.rule_environment_id IN ({placeholders})
        )
        SELECT
            rp.rule_environment_id,
            rp.constant_id,
            rp.compound1_id,
            rp.compound2_id,
            c1.clean_smiles AS compound1_smiles,
            c2.clean_smiles AS compound2_smiles,
            cs.smiles AS constant_smiles
        FROM ranked_pairs rp
        INNER JOIN compound c1 ON c1.id = rp.compound1_id
        INNER JOIN compound c2 ON c2.id = rp.compound2_id
        LEFT JOIN constant_smiles cs ON cs.id = NULLIF(rp.constant_id, -1)
        WHERE rp.rank_in_constant <= ?
        ORDER BY rp.rule_environment_id ASC, rp.constant_id ASC, rp.rank_in_constant ASC
        """
        params: List[Any] = list(chunk)
        params.append(int(per_env_constant_limit))
        cursor.execute(sql, params)
        for row in cursor.fetchall():
            rows.append(
                {
                    "rule_environment_id": int(row["rule_environment_id"]),
                    "constant_id": int(row["constant_id"]),
                    "compound1_id": int(row["compound1_id"]),
                    "compound2_id": int(row["compound2_id"]),
                    "compound1_smiles": str(row["compound1_smiles"] or ""),
                    "compound2_smiles": str(row["compound2_smiles"] or ""),
                    "constant_smiles": str(row["constant_smiles"] or ""),
                }
            )
    return rows


def _load_compound_property_values(
    conn: _DbConnection,
    *,
    compound_ids: List[int],
    property_name: str,
) -> Dict[int, float]:
    token = str(property_name or "").strip()
    if not token or not compound_ids:
        return {}
    cursor = conn.cursor()
    placeholders = ",".join("?" for _ in compound_ids)
    sql = f"""
    SELECT cp.compound_id AS compound_id, cp.value AS value
    FROM compound_property cp
    INNER JOIN property_name pn ON pn.id = cp.property_name_id
    WHERE pn.name = ?
      AND cp.compound_id IN ({placeholders})
    """
    params: List[Any] = [token]
    params.extend(compound_ids)
    cursor.execute(sql, params)
    values: Dict[int, float] = {}
    for row in cursor.fetchall():
        cid = int(row["compound_id"])
        try:
            values[cid] = float(row["value"])
        except Exception:
            continue
    return values


def _smarts_attachment_signature(smarts: str) -> str:
    text = str(smarts or "")
    return f"{text}|att:{text.count('*')}"


def _sanitize_transform_fragment_for_highlight(fragment_smiles: str) -> str:
    text = str(fragment_smiles or "").strip()
    if not text:
        return ""
    try:
        from rdkit import Chem
    except Exception:
        return ""

    mol = Chem.MolFromSmiles(text)
    if mol is None:
        mol = Chem.MolFromSmarts(text)
    if mol is None:
        return ""

    try:
        editable = Chem.RWMol(mol)
        dummy_indices = [
            int(atom.GetIdx())
            for atom in editable.GetAtoms()
            if int(atom.GetAtomicNum()) == 0
        ]
        for atom_idx in sorted(dummy_indices, reverse=True):
            editable.RemoveAtom(atom_idx)
        trimmed = editable.GetMol()
        frags = Chem.GetMolFrags(trimmed, asMols=True, sanitizeFrags=True)
    except Exception:
        return ""

    if not frags:
        return ""

    best = max(frags, key=lambda frag: int(frag.GetNumHeavyAtoms()))
    if int(best.GetNumHeavyAtoms()) <= 0:
        return ""
    try:
        result = Chem.MolToSmiles(best, canonical=True)
    except Exception:
        return ""
    if not result or "*" in result:
        return ""
    return result


def _count_fragment_heavy_atoms(fragment_smiles: str) -> int:
    text = str(fragment_smiles or "").strip()
    if not text:
        return 0
    try:
        from rdkit import Chem
    except Exception:
        return 0
    mol = Chem.MolFromSmiles(text)
    if mol is None:
        mol = Chem.MolFromSmarts(text)
    if mol is None:
        return 0
    return sum(
        1
        for atom in mol.GetAtoms()
        if int(atom.GetAtomicNum()) > 1 and int(atom.GetAtomicNum()) != 0
    )


def _read_dummy_label(atom: Any) -> int:
    try:
        map_num = int(atom.GetAtomMapNum() or 0)
    except Exception:
        map_num = 0
    if map_num > 0:
        return map_num
    try:
        isotope = int(atom.GetIsotope() or 0)
    except Exception:
        isotope = 0
    return isotope if isotope > 0 else 0


def _extract_variable_items_with_atoms(variable_spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = variable_spec.get("items") if isinstance(variable_spec.get("items"), list) else []
    results: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        atom_indices = _normalize_atom_indices(item.get("atom_indices"))
        if not atom_indices:
            continue
        results.append(
            {
                "fragment_id": str(item.get("fragment_id") or "").strip(),
                "query": str(item.get("query") or item.get("smarts") or item.get("smiles") or "").strip(),
                "mode": str(item.get("mode") or "").strip().lower(),
                "atom_indices": atom_indices,
            }
        )
    return results


def _build_query_replacement_contexts(query_mol: str, variable_spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    contexts: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in _extract_variable_items_with_atoms(variable_spec):
        atom_indices = item.get("atom_indices") if isinstance(item.get("atom_indices"), list) else []
        context = _prepare_query_replacement_context(query_mol, atom_indices)
        if context is None:
            continue
        variable_smiles = str(context.get("variable_smiles") or "").strip()
        scaffold_smiles = str(context.get("scaffold_smiles") or "").strip()
        if not variable_smiles or not scaffold_smiles:
            continue
        context_key = f"{variable_smiles}||{scaffold_smiles}"
        if context_key in seen:
            continue
        seen.add(context_key)
        context_payload = dict(context)
        context_payload["fragment_id"] = str(item.get("fragment_id") or "").strip()
        context_payload["query"] = str(item.get("query") or "").strip()
        context_payload["attachment_count"] = max(1, variable_smiles.count("*"))
        context_payload["variable_heavy_atoms"] = _count_fragment_heavy_atoms(variable_smiles)
        contexts.append(context_payload)
    return contexts


def _resolve_candidate_query_context(
    from_smiles: str,
    query_contexts: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if not query_contexts:
        return None
    from_text = str(from_smiles or "").strip()
    if not from_text:
        return None

    exact_matches = [
        context
        for context in query_contexts
        if _attachment_queries_equivalent(
            from_text,
            str(context.get("variable_smiles") or "").strip(),
        )
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        exact_matches.sort(
            key=lambda item: (
                int(item.get("attachment_count") or 0),
                int(item.get("variable_heavy_atoms") or 0),
                str(item.get("fragment_id") or ""),
            )
        )
        return exact_matches[0]

    attachment_count = max(1, from_text.count("*"))
    same_attachment = [
        context
        for context in query_contexts
        if int(context.get("attachment_count") or 0) == attachment_count
    ]
    if not same_attachment:
        return None
    if len(same_attachment) == 1:
        return same_attachment[0]

    from_heavy_atoms = _count_fragment_heavy_atoms(from_text)
    same_attachment.sort(
        key=lambda item: (
            abs(int(item.get("variable_heavy_atoms") or 0) - from_heavy_atoms),
            str(item.get("fragment_id") or ""),
        )
    )
    return same_attachment[0]


def _prepare_query_replacement_context(query_mol: str, atom_indices: List[int]) -> Optional[Dict[str, Any]]:
    try:
        from rdkit import Chem
    except Exception:
        return None
    query_text = str(query_mol or "").strip()
    if not query_text:
        return None
    parent = Chem.MolFromSmiles(query_text)
    if parent is None:
        return None

    atom_set = {
        int(idx)
        for idx in atom_indices
        if isinstance(idx, int) and 0 <= int(idx) < parent.GetNumAtoms()
    }
    if not atom_set:
        return None

    boundary_records: Dict[int, Tuple[int, int, int]] = {}
    for atom_idx in atom_set:
        atom = parent.GetAtomWithIdx(atom_idx)
        for neighbor in atom.GetNeighbors():
            neighbor_idx = int(neighbor.GetIdx())
            if neighbor_idx in atom_set:
                continue
            bond = parent.GetBondBetweenAtoms(atom_idx, neighbor_idx)
            if bond is None:
                continue
            bond_idx = int(bond.GetIdx())
            current = boundary_records.get(bond_idx)
            if current is None:
                boundary_records[bond_idx] = (bond_idx, atom_idx, neighbor_idx)
                continue
            if neighbor_idx < current[2] or (neighbor_idx == current[2] and atom_idx < current[1]):
                boundary_records[bond_idx] = (bond_idx, atom_idx, neighbor_idx)
    if not boundary_records:
        return None

    boundary = sorted(boundary_records.values(), key=lambda item: (item[2], item[1], item[0]))
    bond_indices = [item[0] for item in boundary]
    labels = list(range(1, len(boundary) + 1))
    dummy_labels = [(label, label) for label in labels]
    fragmented = Chem.FragmentOnBonds(parent, bond_indices, addDummies=True, dummyLabels=dummy_labels)
    for atom in fragmented.GetAtoms():
        if int(atom.GetAtomicNum()) != 0:
            continue
        label = _read_dummy_label(atom)
        if label > 0:
            atom.SetAtomMapNum(label)
            atom.SetIsotope(0)

    fragments = Chem.GetMolFrags(fragmented, asMols=False, sanitizeFrags=False)
    selected_atoms: Optional[set[int]] = None
    best_overlap = -1
    for frag_atoms in fragments:
        frag_set = {int(idx) for idx in frag_atoms}
        overlap = len(frag_set.intersection(atom_set))
        if overlap > best_overlap:
            best_overlap = overlap
            selected_atoms = frag_set
    if not selected_atoms or best_overlap <= 0:
        return None

    all_atom_indices = set(range(fragmented.GetNumAtoms()))
    scaffold_atoms = sorted(int(idx) for idx in all_atom_indices.difference(selected_atoms))
    if not scaffold_atoms:
        return None
    variable_atoms = sorted(int(idx) for idx in selected_atoms)

    try:
        variable_smiles = Chem.MolFragmentToSmiles(fragmented, atomsToUse=variable_atoms, canonical=True)
        scaffold_smiles = Chem.MolFragmentToSmiles(fragmented, atomsToUse=scaffold_atoms, canonical=True)
    except Exception:
        return None
    if not variable_smiles or not scaffold_smiles or "*" not in variable_smiles or "*" not in scaffold_smiles:
        return None
    return {
        "query_smiles": Chem.MolToSmiles(parent, canonical=True),
        "variable_smiles": variable_smiles,
        "scaffold_smiles": scaffold_smiles,
        "attachment_count": len(boundary),
    }


def _clear_dummy_labels_for_matching(mol: Any) -> None:
    for atom in mol.GetAtoms():
        if int(atom.GetAtomicNum()) != 0:
            continue
        atom.SetAtomMapNum(0)
        atom.SetIsotope(0)


def _build_attachment_label_mapping(from_smiles: str, query_variable_smiles: str) -> Optional[Dict[int, int]]:
    try:
        from rdkit import Chem
    except Exception:
        return None

    from_mol = Chem.MolFromSmiles(str(from_smiles or "").strip())
    query_mol = Chem.MolFromSmiles(str(query_variable_smiles or "").strip())
    if from_mol is None or query_mol is None:
        return None

    from_dummy_labels: Dict[int, int] = {}
    query_dummy_labels: Dict[int, int] = {}
    for atom in from_mol.GetAtoms():
        if int(atom.GetAtomicNum()) == 0:
            label = _read_dummy_label(atom)
            if label > 0:
                from_dummy_labels[int(atom.GetIdx())] = label
    for atom in query_mol.GetAtoms():
        if int(atom.GetAtomicNum()) == 0:
            label = _read_dummy_label(atom)
            if label > 0:
                query_dummy_labels[int(atom.GetIdx())] = label
    if not from_dummy_labels or len(from_dummy_labels) != len(query_dummy_labels):
        return None

    plain_from = Chem.Mol(from_mol)
    plain_query = Chem.Mol(query_mol)
    _clear_dummy_labels_for_matching(plain_from)
    _clear_dummy_labels_for_matching(plain_query)

    matches = plain_query.GetSubstructMatches(plain_from, uniquify=False)
    for match in matches:
        mapping: Dict[int, int] = {}
        valid = True
        for from_idx, from_label in from_dummy_labels.items():
            query_idx = int(match[int(from_idx)])
            query_label = int(query_dummy_labels.get(query_idx, 0))
            if query_label <= 0:
                valid = False
                break
            mapping[from_label] = query_label
        if valid and len(mapping) == len(from_dummy_labels):
            return mapping
    return None


def _relabel_attachment_fragment(fragment_smiles: str, label_mapping: Dict[int, int]) -> str:
    try:
        from rdkit import Chem
    except Exception:
        return ""
    text = str(fragment_smiles or "").strip()
    if not text:
        return ""
    mol = Chem.MolFromSmiles(text)
    if mol is None:
        return ""
    for atom in mol.GetAtoms():
        if int(atom.GetAtomicNum()) != 0:
            continue
        old_label = _read_dummy_label(atom)
        if old_label <= 0:
            continue
        new_label = int(label_mapping.get(old_label, old_label))
        atom.SetAtomMapNum(new_label)
        atom.SetIsotope(0)
    try:
        return Chem.MolToSmiles(mol, canonical=True)
    except Exception:
        return ""


def _canonicalize_smiles_with_traced_atoms(mol: Any, traced_atom_indices: List[int]) -> Tuple[str, List[int]]:
    try:
        from rdkit import Chem
    except Exception:
        return "", []
    if mol is None:
        return "", []
    valid_indices = sorted(
        {
            int(idx)
            for idx in traced_atom_indices
            if isinstance(idx, int) and 0 <= int(idx) < int(mol.GetNumAtoms())
        }
    )
    trace_base = 900000
    tagged = Chem.Mol(mol)
    for offset, atom_idx in enumerate(valid_indices, start=1):
        atom = tagged.GetAtomWithIdx(int(atom_idx))
        atom.SetAtomMapNum(trace_base + offset)
        atom.SetIsotope(0)
    try:
        tagged_smiles = Chem.MolToSmiles(tagged, canonical=True)
    except Exception:
        return "", []
    tagged_mol = Chem.MolFromSmiles(tagged_smiles)
    if tagged_mol is None:
        return "", []

    template_traced_indices: List[int] = []
    for atom in tagged_mol.GetAtoms():
        if int(atom.GetAtomMapNum() or 0) >= trace_base:
            template_traced_indices.append(int(atom.GetIdx()))

    template_mol = Chem.Mol(tagged_mol)
    for atom in template_mol.GetAtoms():
        atom.SetAtomMapNum(0)
        atom.SetIsotope(0)

    try:
        final_smiles = Chem.MolToSmiles(template_mol, canonical=True)
    except Exception:
        return "", []
    final_mol = Chem.MolFromSmiles(final_smiles)
    if final_mol is None:
        return "", []

    # Map traced atoms onto the atom indexing of the final canonical SMILES.
    matches = final_mol.GetSubstructMatches(template_mol, uniquify=False)
    if not matches:
        return final_smiles, []
    mapped_candidates: List[Tuple[int, ...]] = []
    for match in matches:
        mapped = tuple(sorted(int(match[idx]) for idx in template_traced_indices if 0 <= int(idx) < len(match)))
        if mapped:
            mapped_candidates.append(mapped)
    if not mapped_candidates:
        return final_smiles, []
    mapped_candidates.sort()
    return final_smiles, list(mapped_candidates[0])


def _stitch_scaffold_and_replacement(scaffold_smiles: str, replacement_smiles: str) -> Dict[str, Any]:
    try:
        from rdkit import Chem
    except Exception:
        return {}
    scaffold = Chem.MolFromSmiles(str(scaffold_smiles or "").strip())
    replacement = Chem.MolFromSmiles(str(replacement_smiles or "").strip())
    if scaffold is None or replacement is None:
        return {}

    scaffold_atom_count = scaffold.GetNumAtoms()
    combo = Chem.CombineMols(scaffold, replacement)
    rw = Chem.RWMol(combo)
    replacement_real_atom_ids: List[int] = []
    for atom in replacement.GetAtoms():
        if int(atom.GetAtomicNum()) == 0:
            continue
        replacement_real_atom_ids.append(scaffold_atom_count + int(atom.GetIdx()))
    dummies_by_label: Dict[int, List[int]] = {}
    for atom in rw.GetAtoms():
        if int(atom.GetAtomicNum()) != 0:
            continue
        label = _read_dummy_label(atom)
        if label <= 0:
            continue
        dummies_by_label.setdefault(label, []).append(int(atom.GetIdx()))
    if not dummies_by_label:
        return {}

    to_remove: List[int] = []
    for label, atom_ids in dummies_by_label.items():
        scaffold_dummy = next((idx for idx in atom_ids if idx < scaffold_atom_count), None)
        replacement_dummy = next((idx for idx in atom_ids if idx >= scaffold_atom_count), None)
        if scaffold_dummy is None or replacement_dummy is None:
            continue

        scaffold_atom = rw.GetAtomWithIdx(scaffold_dummy)
        replacement_atom = rw.GetAtomWithIdx(replacement_dummy)
        scaffold_neighbors = [int(n.GetIdx()) for n in scaffold_atom.GetNeighbors()]
        replacement_neighbors = [int(n.GetIdx()) for n in replacement_atom.GetNeighbors()]
        if len(scaffold_neighbors) != 1 or len(replacement_neighbors) != 1:
            continue
        left = scaffold_neighbors[0]
        right = replacement_neighbors[0]
        if left == right:
            continue

        left_bond = rw.GetBondBetweenAtoms(scaffold_dummy, left)
        right_bond = rw.GetBondBetweenAtoms(replacement_dummy, right)
        bond_type = Chem.BondType.SINGLE
        if right_bond is not None and right_bond.GetBondType() != Chem.BondType.SINGLE:
            bond_type = right_bond.GetBondType()
        elif left_bond is not None and left_bond.GetBondType() != Chem.BondType.SINGLE:
            bond_type = left_bond.GetBondType()
        if rw.GetBondBetweenAtoms(left, right) is None:
            rw.AddBond(left, right, bond_type)
        to_remove.extend([scaffold_dummy, replacement_dummy])

    if not to_remove:
        return {}

    removed = sorted(set(to_remove))
    removed_set = set(removed)
    replacement_after_remove: List[int] = []
    for old_idx in replacement_real_atom_ids:
        if old_idx in removed_set:
            continue
        replacement_after_remove.append(int(old_idx) - bisect_left(removed, int(old_idx)))

    for atom_idx in sorted(removed_set, reverse=True):
        rw.RemoveAtom(atom_idx)

    result = rw.GetMol()
    try:
        Chem.SanitizeMol(result)
    except Exception:
        return {}
    final_smiles, canonical_replacement_indices = _canonicalize_smiles_with_traced_atoms(result, replacement_after_remove)
    if not final_smiles:
        return {}
    return {
        "final_smiles": final_smiles,
        "replacement_atom_indices": canonical_replacement_indices,
    }


def _generate_query_specific_final_smiles(
    *,
    query_context: Dict[str, Any],
    from_smiles: str,
    to_smiles: str,
) -> Dict[str, Any]:
    from_text = str(from_smiles or "").strip()
    to_text = str(to_smiles or "").strip()
    if not from_text or not to_text:
        return {}
    query_variable_smiles = str(query_context.get("variable_smiles") or "").strip()
    scaffold_smiles = str(query_context.get("scaffold_smiles") or "").strip()
    if not query_variable_smiles or not scaffold_smiles:
        return {}
    if max(1, from_text.count("*")) != max(1, query_variable_smiles.count("*")):
        return {}
    if max(1, to_text.count("*")) != max(1, query_variable_smiles.count("*")):
        return {}

    label_mapping = _build_attachment_label_mapping(from_text, query_variable_smiles)
    if not label_mapping:
        return {}
    relabeled_to = _relabel_attachment_fragment(to_text, label_mapping)
    if not relabeled_to:
        return {}
    return _stitch_scaffold_and_replacement(scaffold_smiles, relabeled_to)


def _apply_variable_constant_filters_to_rows(
    rows: List[Dict[str, Any]],
    constant_query: str,
) -> List[Dict[str, Any]]:
    if not rows:
        return []
    try:
        from rdkit import Chem
    except Exception:
        return rows

    constant_pattern = None
    if constant_query:
        constant_pattern = Chem.MolFromSmarts(constant_query) or Chem.MolFromSmiles(constant_query)

    filtered: List[Dict[str, Any]] = []
    for row in rows:
        final_smiles = str(row.get("final_smiles") or "").strip()
        if not final_smiles:
            continue
        mol = Chem.MolFromSmiles(final_smiles)
        if not mol:
            continue
        if constant_pattern is not None and not mol.HasSubstructMatch(constant_pattern):
            continue
        filtered.append(row)
    return filtered


def _percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = (len(ordered) - 1) * p
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return ordered[lo]
    weight = idx - lo
    return ordered[lo] * (1.0 - weight) + ordered[hi] * weight


def _aggregate_mmp_transforms(rows: List[Dict[str, Any]], direction: str = "increase") -> List[Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        transform_id = str(row.get("transform_id") or "").strip()
        if not transform_id:
            continue
        entry = grouped.setdefault(
            transform_id,
            {
                "transform_id": transform_id,
                "from_smiles": row.get("from_smiles", ""),
                "to_smiles": row.get("to_smiles", ""),
                "to_highlight_smiles": row.get("to_highlight_smiles", ""),
                "from_num_frags": int(row.get("from_num_frags", 0) or max(1, str(row.get("from_smiles", "")).count("*"))),
                "to_num_frags": int(row.get("to_num_frags", 0) or max(1, str(row.get("to_smiles", "")).count("*"))),
                "constant_smiles": row.get("constant_smiles", ""),
                "rule_env_radius": row.get("rule_env_radius", 0),
                "_n_pairs_values": [],
                "_deltas": [],
                "examples": [],
            },
        )
        entry["_n_pairs_values"].append(int(row.get("n_pairs", 1) or 1))
        entry["_deltas"].append(float(row.get("median_delta", 0.0) or 0.0))
        if len(entry["examples"]) < 5:
            entry["examples"].append(row.get("final_smiles", ""))

    transforms: List[Dict[str, Any]] = []
    for entry in grouped.values():
        n_pairs_values = entry.pop("_n_pairs_values", [])
        deltas = entry.pop("_deltas", [])
        n_pairs = max(n_pairs_values) if n_pairs_values else 0
        median_delta = _percentile(deltas, 0.5)
        q1 = _percentile(deltas, 0.25)
        q3 = _percentile(deltas, 0.75)
        iqr = q3 - q1
        if deltas:
            mean_delta = sum(deltas) / len(deltas)
            std = (sum((value - mean_delta) ** 2 for value in deltas) / len(deltas)) ** 0.5
        else:
            std = 0.0
        improved = sum(1 for value in deltas if (value < 0 if direction == "decrease" else value > 0))
        directionality = (improved / len(deltas)) if deltas else 0.0
        evidence_strength = (n_pairs / (1.0 + abs(std) + abs(iqr))) if n_pairs > 0 else 0.0
        transforms.append(
            {
                **entry,
                "n_pairs": n_pairs,
                "median_delta": median_delta,
                "iqr": iqr,
                "std": std,
                "directionality": directionality,
                "evidence_strength": evidence_strength,
            }
        )

    transforms.sort(key=lambda item: (item.get("evidence_strength", 0.0), item.get("n_pairs", 0)), reverse=True)
    return transforms


def _build_mmp_clusters(
    transforms: List[Dict[str, Any]],
    group_by: str = "to",
    min_pairs: int = 1,
    direction: str = "increase",
) -> List[Dict[str, Any]]:
    key_field = "to_smiles" if group_by == "to" else "from_smiles" if group_by == "from" else "rule_env_radius"
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in transforms:
        if int(item.get("n_pairs", 0) or 0) < min_pairs:
            continue
        key = str(item.get(key_field, ""))
        grouped.setdefault(key, []).append(item)

    clusters: List[Dict[str, Any]] = []
    for key, items in grouped.items():
        cluster_size = len(items)
        deltas = [float(i.get("median_delta", 0.0) or 0.0) for i in items]
        improved = sum(1 for value in deltas if (value < 0 if direction == "decrease" else value > 0))
        clusters.append(
            {
                "cluster_id": hashlib.sha1(f"{group_by}:{key}".encode("utf-8")).hexdigest()[:16],
                "group_by": group_by,
                "group_key": key,
                "cluster_size": cluster_size,
                "n_pairs": int(sum(int(i.get("n_pairs", 0) or 0) for i in items)),
                "%median_improved": (100.0 * improved / cluster_size) if cluster_size > 0 else 0.0,
                "median_delta": _percentile(deltas, 0.5),
                "dispersion": _percentile(deltas, 0.75) - _percentile(deltas, 0.25),
                "evidence_strength": float(
                    sum(float(i.get("evidence_strength", 0.0) or 0.0) for i in items) / max(1, cluster_size)
                ),
                "transform_ids": [str(i.get("transform_id")) for i in items],
            }
        )
    clusters.sort(key=lambda item: (item.get("evidence_strength", 0.0), item.get("cluster_size", 0)), reverse=True)
    return clusters


def _build_rows_from_candidates(
    *,
    candidates: List[Dict[str, Any]],
    pair_counts: Dict[Tuple[int, int], int],
    pair_samples: List[Dict[str, Any]],
    requested_property: str,
    query_mol: str,
    query_contexts: List[Dict[str, Any]],
    max_results: int,
    db_property_name: str,
    compound_property_values: Dict[int, float],
) -> List[Dict[str, Any]]:
    if not query_contexts:
        return []
    base_descriptor = _compute_descriptor_value(query_mol, requested_property)
    by_env: Dict[int, List[Dict[str, Any]]] = {}
    for sample in pair_samples:
        by_env.setdefault(int(sample["rule_environment_id"]), []).append(sample)

    rows: List[Dict[str, Any]] = []
    seen_keys: set[str] = set()
    unmatched_context_count = 0
    for candidate in candidates:
        env_id = int(candidate["rule_environment_id"])
        orientation = str(candidate.get("orientation") or "forward")
        sample_rows = by_env.get(env_id, [])

        from_smiles = str(candidate.get("from_smiles") or "")
        to_smiles = str(candidate.get("to_smiles") or "")
        query_context = _resolve_candidate_query_context(from_smiles, query_contexts)
        if query_context is None:
            unmatched_context_count += 1
            continue
        stitched = _generate_query_specific_final_smiles(
            query_context=query_context,
            from_smiles=from_smiles,
            to_smiles=to_smiles,
        )
        final_smiles = str(stitched.get("final_smiles") or "").strip()
        highlighted_atom_indices = _normalize_atom_indices(stitched.get("replacement_atom_indices"))
        if not final_smiles or final_smiles == query_mol:
            continue
        try:
            from rdkit import Chem
        except Exception:
            Chem = None  # type: ignore
        if Chem is not None:
            final_mol = Chem.MolFromSmiles(final_smiles)
            if final_mol is None:
                continue
            atom_upper = int(final_mol.GetNumAtoms()) - 1
            highlighted_atom_indices = [idx for idx in highlighted_atom_indices if idx <= atom_upper]
        expected_highlight_atoms = _count_fragment_heavy_atoms(to_smiles)
        if expected_highlight_atoms > 0 and len(highlighted_atom_indices) != expected_highlight_atoms:
            logger.warning(
                "Lead-opt highlight trace mismatch; dropping candidate rule_env_id=%s expected=%s actual=%s from='%s' to='%s'",
                env_id,
                expected_highlight_atoms,
                len(highlighted_atom_indices),
                from_smiles,
                to_smiles,
            )
            continue

        n_pairs = int(candidate.get("rule_env_pairs", 0) or 0)
        if n_pairs <= 0 and sample_rows:
            n_pairs = max(int(pair_counts.get((env_id, int(sample.get("constant_id", -1))), 0) or 0) for sample in sample_rows)
        n_pairs = max(1, n_pairs)

        constant_smiles = str(query_context.get("scaffold_smiles") or "")
        transform_key = f"{from_smiles}>>{to_smiles}||{constant_smiles}"
        transform_id = hashlib.sha1(transform_key.encode("utf-8")).hexdigest()[:16]
        dedupe_key = "||".join(
            [
                final_smiles,
                from_smiles,
                to_smiles,
                _smarts_attachment_signature(constant_smiles),
                str(candidate.get("rule_env_radius", 0)),
            ]
        )
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        input_property_value: Optional[float] = base_descriptor
        selected_property_value: Optional[float] = _compute_descriptor_value(final_smiles, requested_property)
        median_delta = 0.0
        if selected_property_value is not None and input_property_value is not None:
            median_delta = float(selected_property_value - input_property_value)

        if db_property_name and sample_rows:
            db_deltas: List[float] = []
            for sample in sample_rows:
                if orientation == "reverse":
                    input_compound_id = int(sample.get("compound2_id", 0) or 0)
                    final_compound_id = int(sample.get("compound1_id", 0) or 0)
                else:
                    input_compound_id = int(sample.get("compound1_id", 0) or 0)
                    final_compound_id = int(sample.get("compound2_id", 0) or 0)
                input_value = compound_property_values.get(input_compound_id)
                final_value = compound_property_values.get(final_compound_id)
                if input_value is None or final_value is None:
                    continue
                db_deltas.append(float(final_value - input_value))
            if db_deltas:
                median_delta = _percentile(db_deltas, 0.5)

        row = {
            "transform_id": transform_id,
            "rule_environment_id": env_id,
            "rule_id": int(candidate.get("rule_id", 0) or 0),
            "input_smiles": query_mol,
            "final_smiles": final_smiles,
            "from_smiles": from_smiles,
            "to_smiles": to_smiles,
            "to_highlight_smiles": _sanitize_transform_fragment_for_highlight(to_smiles),
            "final_highlight_atom_indices": highlighted_atom_indices,
            "from_num_frags": int(candidate.get("from_num_frags", 0) or max(1, from_smiles.count("*"))),
            "to_num_frags": int(candidate.get("to_num_frags", 0) or max(1, to_smiles.count("*"))),
            "constant_smiles": constant_smiles,
            "n_pairs": n_pairs,
            "median_delta": median_delta,
            "selected_property": requested_property if selected_property_value is not None else "",
            "selected_property_value": float(selected_property_value) if selected_property_value is not None else None,
            "input_property_value": float(input_property_value) if input_property_value is not None else None,
            "rule_env_radius": int(candidate.get("rule_env_radius", 0) or 0),
            "resolved_db_property": db_property_name or "",
            "selected_fragment_id": str(query_context.get("fragment_id") or ""),
            "query_variable_smiles": str(query_context.get("variable_smiles") or ""),
        }
        rows.append(row)
        if len(rows) >= max_results:
            break
    if unmatched_context_count > 0:
        logger.info(
            "Lead-opt MMP row stitching skipped %s candidates due to unmatched variable context.",
            unmatched_context_count,
        )
    return rows


def _normalize_env_radius(raw: Any) -> Optional[int]:
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    if isinstance(raw, (int, float)):
        return int(raw)
    return None


def run_mmp_query(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("mmp_query payload must be a JSON object.")

    raw_query_mol = str(payload.get("query_mol") or "").strip()
    query_mol = _normalize_smiles_like(raw_query_mol)
    if not query_mol:
        raise ValueError("'query_mol' is required and must be a valid SMILES.")

    query_mode = str(payload.get("query_mode") or "one-to-many").strip().lower()
    if query_mode not in {"one-to-many", "many-to-many"}:
        query_mode = "one-to-many"

    variable_spec = normalize_variable_spec(payload.get("variable_spec"))
    query_contexts = _build_query_replacement_contexts(raw_query_mol or query_mol, variable_spec)
    if not query_contexts:
        raise ValueError(
            "Variable fragment atom indices are required for replacement stitching. "
            "Please select fragments directly from the 2D map."
        )
    # Keep atom-index mapping tied to the exact incoming ligand text. Canonicalization can reorder atoms.
    variable_queries = build_mmp_query_list_from_variable_spec(variable_spec, query_mol=raw_query_mol or query_mol)
    if not variable_queries:
        raise ValueError(
            "Unable to build attachment-aware variable queries. "
            "Please select a valid variable fragment from the ligand map."
        )
    variable_mode = _resolve_variable_mode(variable_spec)
    selected_fragments = [
        str((item or {}).get("fragment_id") or "").strip()
        for item in (variable_spec.get("items") if isinstance(variable_spec.get("items"), list) else [])
        if str((item or {}).get("fragment_id") or "").strip()
    ]
    variable_num_frags = sorted({max(1, str(query or "").count("*")) for query in variable_queries})
    logger.info(
        "Lead-opt MMP variable resolved: mode=%s fragments=%s contexts=%s num_frags=%s queries=%s",
        variable_mode,
        selected_fragments[:6],
        len(query_contexts),
        variable_num_frags,
        variable_queries[:6],
    )

    constant_spec = _safe_json_object(payload.get("constant_spec"))
    constant_query = str(
        constant_spec.get("query") or constant_spec.get("smarts") or constant_spec.get("smiles") or ""
    ).strip()
    property_targets = _safe_json_object(payload.get("property_targets"))
    requested_property = str(property_targets.get("property") or "").strip().lower()
    direction = str(property_targets.get("direction") or "").strip().lower()
    if direction not in {"increase", "decrease"}:
        direction = "increase"
    max_results = min(1000, max(1, int(payload.get("max_results") or 240)))
    min_pairs = max(1, int(payload.get("min_pairs") or 1))
    env_radius = _normalize_env_radius(payload.get("rule_env_radius"))

    database_path, selected_database = _resolve_runtime_database(payload)
    selected_database_id = str(selected_database.get("id") or "").strip()
    selected_database_label = str(selected_database.get("label") or "").strip()
    selected_database_schema = str(selected_database.get("schema") or "").strip()
    db_backend = "postgres"
    db_log_label = _redact_postgres_dsn(database_path)
    engine_name = "postgres_rule_env_v1"
    resolved_db_property = _resolve_property_for_db(
        requested_property,
        database_path,
        schema_override=selected_database_schema,
    )
    query_start = time.time()
    logger.info(
        "Lead-opt MMP query command: runner=lead_optimization/mmp_query_service.py db=%s db_id=%s schema=%s mode=%s property=%s direction=%s min_pairs=%s max_results=%s env_radius=%s",
        db_log_label,
        selected_database_id or "<default>",
        selected_database_schema or "<default>",
        query_mode,
        requested_property,
        direction,
        min_pairs,
        max_results,
        env_radius,
    )
    if requested_property and not resolved_db_property and requested_property not in {"mw", "logp", "tpsa"}:
        available_properties = _list_available_db_properties(
            database_path,
            schema_override=selected_database_schema,
        )
        logger.warning(
            "Requested property '%s' not found in mmpdb property_name. Available sample: %s",
            requested_property,
            available_properties[:8],
        )
    conn = _open_db_connection(database_path, schema_override=selected_database_schema)
    try:
        has_num_frags = _table_has_column(conn, "rule_smiles", "num_frags")
        has_constant_num_frags = _table_has_column(conn, "constant_smiles", "num_frags")
        if not has_num_frags:
            raise ValueError(
                "MMP database schema is missing attachment metadata on rule_smiles (num_frags). "
                "Please rebuild/enrich via lead_optimization.mmp_lifecycle before running lead optimization queries."
            )
        max_rule_smiles = max(200, min(5000, int(payload.get("max_rule_smiles") or 2000)))
        rule_smiles_ids = _lookup_rule_smiles_ids(
            conn,
            variable_queries=variable_queries,
            variable_mode=variable_mode,
            max_rule_smiles=max_rule_smiles,
        )
        logger.info(
            "Lead-opt MMP lookup: matched_rule_smiles=%s variable_mode=%s queries=%s",
            len(rule_smiles_ids),
            variable_mode,
            min(6, len(variable_queries)),
        )
        if not rule_smiles_ids:
            raise ValueError(
                "No MMP rule fragments matched the selected variable fragment. "
                "Try selecting a smaller fragment or switching variable mode."
            )

        max_env_rows = max(300, min(5000, int(payload.get("max_env_rows") or max_results * 20)))
        candidates = _fetch_rule_environment_candidates(
            conn,
            rule_smiles_ids=rule_smiles_ids,
            min_pairs=min_pairs,
            max_env_rows=max_env_rows,
        )
        logger.info(
            "Lead-opt MMP lookup: candidate_rule_environments=%s max_env_rows=%s",
            len(candidates),
            max_env_rows,
        )
        if not candidates:
            return {
                "query_mol": query_mol,
                "query_mode": query_mode,
                "variable_spec": variable_spec,
                "constant_spec": constant_spec,
                "property_targets": property_targets,
                "mmp_database_id": selected_database_id,
                "mmp_database_label": selected_database_label,
                "mmp_database_schema": selected_database_schema,
                "requested_property": requested_property,
                "resolved_db_property": resolved_db_property,
                "direction": direction,
                "rule_env_radius": env_radius,
                "rows": [],
                "global_rows": [],
                "transforms": [],
                "global_transforms": [],
                "clusters": [],
                "min_pairs": min_pairs,
                "stats": {
                    "global_rows": 0,
                    "environment_rows": 0,
                    "matched_rule_smiles": len(rule_smiles_ids),
                    "matched_rule_environments": 0,
                    "query_num_frags": variable_num_frags,
                    "query_contexts": len(query_contexts),
                    "resolved_db_property": resolved_db_property,
                    "matched_variable_mode": variable_mode,
                    "rule_smiles_has_num_frags": has_num_frags,
                    "constant_smiles_has_num_frags": has_constant_num_frags,
                    "engine": engine_name,
                },
            }

        env_ids = sorted({int(item["rule_environment_id"]) for item in candidates})
        pair_counts = _fetch_pair_counts(conn, rule_environment_ids=env_ids)
        pair_samples = _fetch_pair_samples(
            conn,
            rule_environment_ids=env_ids,
            per_env_constant_limit=max(1, min(4, int(payload.get("pairs_per_constant") or 2))),
        )
        compound_property_values: Dict[int, float] = {}
        if resolved_db_property:
            compound_ids = sorted(
                {
                    int(sample.get("compound1_id", 0) or 0)
                    for sample in pair_samples
                    if int(sample.get("compound1_id", 0) or 0) > 0
                }
                | {
                    int(sample.get("compound2_id", 0) or 0)
                    for sample in pair_samples
                    if int(sample.get("compound2_id", 0) or 0) > 0
                }
            )
            compound_property_values = _load_compound_property_values(
                conn,
                compound_ids=compound_ids,
                property_name=resolved_db_property,
            )
        rows = _build_rows_from_candidates(
            candidates=candidates,
            pair_counts=pair_counts,
            pair_samples=pair_samples,
            requested_property=requested_property,
            query_mol=query_mol,
            query_contexts=query_contexts,
            max_results=max_results,
            db_property_name=resolved_db_property,
            compound_property_values=compound_property_values,
        )
        if candidates and not rows:
            raise ValueError(
                "Matched MMP transforms could not be applied to the selected fragment. "
                "Please reselect fragment atoms and retry."
            )
    finally:
        conn.close()

    filtered_rows = _apply_variable_constant_filters_to_rows(rows, constant_query=constant_query)
    global_transforms = _aggregate_mmp_transforms(filtered_rows, direction=direction)
    env_rows = filtered_rows
    if env_radius is not None:
        env_rows = [row for row in filtered_rows if int(row.get("rule_env_radius", 0) or 0) <= env_radius]
    transforms = _aggregate_mmp_transforms(env_rows, direction=direction)
    clusters = _build_mmp_clusters(
        transforms,
        group_by="to" if query_mode == "many-to-many" else "to",
        min_pairs=min_pairs,
        direction=direction,
    )

    elapsed = max(0.0, float(time.time() - query_start))
    logger.info(
        "Lead-opt MMP query completed: rows=%s global_rows=%s clusters=%s elapsed=%.3fs",
        len(env_rows),
        len(filtered_rows),
        len(clusters),
        elapsed,
    )
    return {
        "query_mol": query_mol,
        "query_mode": query_mode,
        "variable_spec": variable_spec,
        "constant_spec": constant_spec,
        "property_targets": property_targets,
        "mmp_database_id": selected_database_id,
        "mmp_database_label": selected_database_label,
        "mmp_database_schema": selected_database_schema,
        "requested_property": requested_property,
        "resolved_db_property": resolved_db_property,
        "direction": direction,
        "rule_env_radius": env_radius,
        "rows": env_rows,
        "global_rows": filtered_rows,
        "transforms": transforms,
        "global_transforms": global_transforms,
        "clusters": clusters,
        "min_pairs": min_pairs,
        "stats": {
            "elapsed_seconds": elapsed,
            "global_rows": len(filtered_rows),
            "environment_rows": len(env_rows),
            "n_queries": len(variable_queries),
            "matched_rule_smiles": len(rule_smiles_ids),
            "matched_rule_environments": len({int(row.get("rule_environment_id", 0) or 0) for row in rows}),
            "query_num_frags": variable_num_frags,
            "query_contexts": len(query_contexts),
            "resolved_db_property": resolved_db_property,
            "matched_variable_mode": variable_mode,
            "rule_smiles_has_num_frags": has_num_frags,
            "constant_smiles_has_num_frags": has_constant_num_frags,
            "db_path": db_log_label,
            "engine": engine_name,
        },
    }
