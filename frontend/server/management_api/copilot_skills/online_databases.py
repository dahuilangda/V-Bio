from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List
from urllib.parse import quote

import requests


@dataclass(frozen=True)
class OnlineSkillDefinition:
    name: str
    description: str
    input_schema: Dict[str, Any]


class OnlineDatabaseSkills:
    """Registry of read-only, atomic online-database skills."""

    def __init__(
        self,
        *,
        session: requests.Session,
        timeout_seconds: float = 8.0,
        cache_ttl_seconds: float = 600.0,
    ) -> None:
        self._session = session
        self._timeout_seconds = max(1.0, min(30.0, float(timeout_seconds)))
        self._cache_ttl_seconds = max(1.0, min(3600.0, float(cache_ttl_seconds)))
        self._cache: Dict[str, tuple[float, Dict[str, Any]]] = {}
        self._cache_lock = threading.Lock()
        self._definitions: Dict[str, OnlineSkillDefinition] = {}
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {}
        self._register_builtin_skills()

    @property
    def definitions(self) -> List[OnlineSkillDefinition]:
        return list(self._definitions.values())

    def register(
        self,
        definition: OnlineSkillDefinition,
        handler: Callable[[Dict[str, Any]], Dict[str, Any]],
    ) -> None:
        normalized_name = str(definition.name or "").strip()
        if not normalized_name:
            raise ValueError("Online skill name is required.")
        self._definitions[normalized_name] = definition
        self._handlers[normalized_name] = handler

    def execute(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        normalized_name = str(name or "").strip()
        handler = self._handlers.get(normalized_name)
        if handler is None:
            raise ValueError(f"Unknown online skill: {normalized_name}")
        normalized_arguments = arguments if isinstance(arguments, dict) else {}
        cache_key = json.dumps(
            {"skill": normalized_name, "arguments": normalized_arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        now = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached and cached[0] > now:
                return dict(cached[1])
            if cached:
                self._cache.pop(cache_key, None)
        result = handler(normalized_arguments)
        with self._cache_lock:
            if len(self._cache) >= 512:
                oldest_key = min(self._cache, key=lambda key: self._cache[key][0])
                self._cache.pop(oldest_key, None)
            self._cache[cache_key] = (time.monotonic() + self._cache_ttl_seconds, dict(result))
        return result

    def render_tool_schema(self) -> str:
        return json.dumps(
            [
                {
                    "name": definition.name,
                    "description": definition.description,
                    "read_only": True,
                    "input_schema": definition.input_schema,
                }
                for definition in self.definitions
            ],
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    def _register_builtin_skills(self) -> None:
        identifier_schema = {
            "type": "object",
            "properties": {"identifier": {"type": "string", "minLength": 1, "maxLength": 256}},
            "required": ["identifier"],
            "additionalProperties": False,
        }
        self.register(
            OnlineSkillDefinition(
                name="uniprot.resolve",
                description="Resolve one UniProt identifier to its authoritative protein sequence and metadata.",
                input_schema=identifier_schema,
            ),
            self._resolve_uniprot,
        )
        self.register(
            OnlineSkillDefinition(
                name="pubchem.resolve",
                description="Resolve one PubChem compound identifier or name to authoritative compound metadata and SMILES.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "identifier": {"type": "string", "minLength": 1, "maxLength": 512},
                        "namespace": {"type": "string", "enum": ["cid", "name"]},
                    },
                    "required": ["identifier", "namespace"],
                    "additionalProperties": False,
                },
            ),
            self._resolve_pubchem,
        )
        self.register(
            OnlineSkillDefinition(
                name="rcsb.resolve",
                description="Resolve one RCSB PDB identifier to authoritative entry metadata and structure file links.",
                input_schema=identifier_schema,
            ),
            self._resolve_rcsb,
        )

    def _get_json(self, url: str) -> Dict[str, Any]:
        response = self._session.get(
            url,
            headers={"Accept": "application/json", "User-Agent": "V-Bio-Copilot/1.0"},
            timeout=self._timeout_seconds,
        )
        if not response.ok:
            detail = " ".join(str(getattr(response, "text", "") or "").split())[:240]
            raise RuntimeError(f"HTTP {response.status_code}{': ' + detail if detail else ''}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Online database returned a non-object JSON response.")
        return payload

    @staticmethod
    def _nested_text(value: Any, *paths: tuple[str, ...]) -> str:
        for path in paths:
            current = value
            for key in path:
                if not isinstance(current, dict):
                    current = None
                    break
                current = current.get(key)
            if isinstance(current, str) and current.strip():
                return current.strip()
        return ""

    def _resolve_uniprot(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        identifier = str(arguments.get("identifier") or "").strip()
        url = f"https://rest.uniprot.org/uniprotkb/{quote(identifier, safe='-')}.json"
        payload = self._get_json(url)
        sequence_data = payload.get("sequence") if isinstance(payload.get("sequence"), dict) else {}
        sequence = "".join(str(sequence_data.get("value") or "").split()).upper()
        if not sequence:
            raise RuntimeError("UniProt record does not contain a sequence.")
        genes = payload.get("genes") if isinstance(payload.get("genes"), list) else []
        gene_name = self._nested_text(genes[0], ("geneName", "value")) if genes and isinstance(genes[0], dict) else ""
        description = payload.get("proteinDescription")
        protein_name = self._nested_text(
            description,
            ("recommendedName", "fullName", "value"),
        )
        accession = str(payload.get("primaryAccession") or identifier).strip().upper()
        return {
            "source": "uniprot",
            "identifier": accession,
            "sourceUrl": f"https://www.uniprot.org/uniprotkb/{quote(accession, safe='-')}/entry",
            "accession": accession,
            "label": gene_name or accession,
            "entryName": str(payload.get("uniProtkbId") or accession).strip(),
            "proteinName": protein_name or gene_name or accession,
            "sequence": sequence,
        }

    def _resolve_pubchem(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        identifier = str(arguments.get("identifier") or "").strip()
        namespace = str(arguments.get("namespace") or "").strip().lower()
        property_names = "Title,CanonicalSMILES,IsomericSMILES"
        url = (
            "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/"
            f"{quote(namespace, safe='')}/{quote(identifier, safe='')}/property/{property_names}/JSON"
        )
        payload = self._get_json(url)
        table = payload.get("PropertyTable") if isinstance(payload.get("PropertyTable"), dict) else {}
        properties = table.get("Properties") if isinstance(table.get("Properties"), list) else []
        row = properties[0] if properties and isinstance(properties[0], dict) else {}
        smiles = next(
            (str(row.get(key) or "").strip() for key in ("IsomericSMILES", "SMILES", "CanonicalSMILES", "ConnectivitySMILES") if str(row.get(key) or "").strip()),
            "",
        )
        if not smiles:
            raise RuntimeError("PubChem record does not contain a SMILES property.")
        cid = str(row.get("CID") or identifier).strip()
        return {
            "source": "pubchem",
            "identifier": cid,
            "sourceUrl": f"https://pubchem.ncbi.nlm.nih.gov/compound/{quote(cid, safe='')}",
            "cid": cid,
            "label": str(row.get("Title") or f"CID {cid}").strip(),
            "smiles": smiles,
        }

    def _resolve_rcsb(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        identifier = str(arguments.get("identifier") or "").strip().upper()
        payload = self._get_json(
            f"https://data.rcsb.org/rest/v1/core/entry/{quote(identifier, safe='')}"
        )
        struct = payload.get("struct") if isinstance(payload.get("struct"), dict) else {}
        info = payload.get("rcsb_entry_info") if isinstance(payload.get("rcsb_entry_info"), dict) else {}
        return {
            "source": "rcsb",
            "identifier": identifier,
            "sourceUrl": f"https://www.rcsb.org/structure/{quote(identifier, safe='')}",
            "pdbId": identifier,
            "title": str(struct.get("title") or identifier).strip(),
            "polymerEntityCount": int(info.get("polymer_entity_count") or 0),
            "nonpolymerEntityCount": int(info.get("nonpolymer_entity_count") or 0),
            "cifUrl": f"https://files.rcsb.org/download/{quote(identifier, safe='')}.cif",
            "pdbUrl": f"https://files.rcsb.org/download/{quote(identifier, safe='')}.pdb",
        }
