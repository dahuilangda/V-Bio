from __future__ import annotations

import copy
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


def _required_string(arguments: Dict[str, Any], key: str, *, label: str) -> str:
    """Return a trimmed non-empty string argument, or raise ValueError.

    Centralizes the empty-input guard shared by every search/resolve skill so handlers focus on
    fetch + shape. The input_schema already enforces minLength server-side; this defends a direct
    (non-grammar) call and gives a single honest error path.
    """
    value = str(arguments.get(key) or "").strip()
    if not value:
        raise ValueError(f"{label} requires a non-empty {key}.")
    return value


def _resolve_size(arguments: Dict[str, Any], default: int) -> int:
    """Clamp a skill's optional ``size`` argument to the shared [1, 10] result window."""
    raw = arguments.get("size")
    try:
        return max(1, min(10, int(raw))) if raw is not None else default
    except (TypeError, ValueError):
        return default


def _pubdate_year(value: Any) -> str:
    """Extract the first whitespace/delimiter token of a PubMed pubdate as the year.

    PubMed esummary entries occasionally omit ``pubdate`` (books, preprints, corrections); a naive
    ``"".split()[0]`` would raise IndexError and fail the whole search, so this guards the empty case.
    """
    parts = str(value or "").split()
    return parts[0].split(";")[0] if parts else ""


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
                return copy.deepcopy(cached[1])
            if cached:
                self._cache.pop(cache_key, None)
        result = handler(normalized_arguments)
        with self._cache_lock:
            if len(self._cache) >= 512:
                oldest_key = min(self._cache, key=lambda key: self._cache[key][0])
                self._cache.pop(oldest_key, None)
            self._cache[cache_key] = (time.monotonic() + self._cache_ttl_seconds, copy.deepcopy(result))
        return result

    def _register_builtin_skills(self) -> None:
        identifier_schema = {
            "type": "object",
            "properties": {"identifier": {"type": "string", "minLength": 1, "maxLength": 256}},
            "required": ["identifier"],
            "additionalProperties": False,
        }
        self.register(
            OnlineSkillDefinition(
                name="uniprot.search",
                description=(
                    "Search UniProtKB and return ranked candidate entries, each with its accession and protein "
                    "sequence, so a sequence question can be answered in one step. Pass a UniProt query string "
                    "using the database's native field syntax: gene:<symbol>, protein_name:\"<name>\", "
                    "organism_id:<NCBI taxon id> (9606 is human), reviewed:true, accession:<id>. Combine fields "
                    "with AND. Prefer organism_id over a free-text organism name. Returns no results when there "
                    "is no authoritative match."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1, "maxLength": 256},
                        "size": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
            self._search_uniprot,
        )
        self.register(
            OnlineSkillDefinition(
                name="uniprot.resolve",
                description="Resolve one UniProt accession or entry name to its authoritative protein sequence and metadata.",
                input_schema=identifier_schema,
            ),
            self._resolve_uniprot,
        )
        self.register(
            OnlineSkillDefinition(
                name="pubchem.search",
                description=(
                    "Look up one PubChem compound and return its SMILES, molecular formula, InChIKey, and InChI. "
                    "Choose the namespace that matches the identifier: 'name' for a compound name, synonym, or "
                    "vendor / internal compound code; 'cid' for a numeric PubChem CID; 'smiles' for a SMILES "
                    "string; 'inchi' for an InChI string; 'inchikey' for an InChIKey. Defaults to 'name'. Returns "
                    "no results when there is no authoritative match."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "identifier": {"type": "string", "minLength": 1, "maxLength": 512},
                        "namespace": {
                            "type": "string",
                            "enum": ["name", "cid", "smiles", "inchi", "inchikey"],
                        },
                    },
                    "required": ["identifier"],
                    "additionalProperties": False,
                },
            ),
            self._search_pubchem,
        )
        self.register(
            OnlineSkillDefinition(
                name="rcsb.resolve",
                description="Resolve one RCSB PDB identifier to authoritative entry metadata and structure file links.",
                input_schema=identifier_schema,
            ),
            self._resolve_rcsb,
        )
        self.register(
            OnlineSkillDefinition(
                name="rcsb.search",
                description=(
                    "Search RCSB PDB by free text (protein or gene name, organism, complex description) and return "
                    "ranked matching structure entries with title, experimental method, resolution, and download "
                    "links. Use this to FIND structures by name; use rcsb.resolve only for an exact PDB id. Returns "
                    "no results when there is no match."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "minLength": 1, "maxLength": 256},
                        "size": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
            ),
            self._search_rcsb,
        )
        self.register(
            OnlineSkillDefinition(
                name="alphafold.resolve",
                description=(
                    "Resolve one UniProt accession to its AlphaFold DB predicted structure: model confidence "
                    "(pLDDT), and PDB / mmCIF download links. Complements rcsb (experimental) and uniprot "
                    "(sequence). Use a UniProt accession, not a gene name."
                ),
                input_schema=identifier_schema,
            ),
            self._resolve_alphafold,
        )
        self.register(
            OnlineSkillDefinition(
                name="chembl.bioactivity",
                description=(
                    "Look up a compound's known biological activity from ChEMBL: search the compound by name, then "
                    "return its ranked targets with measured potency (IC50 / Ki / Kd / EC50). Use this to answer "
                    "what targets a drug hits or how potent it is. Returns no results when the compound or its "
                    "activity is unknown."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1, "maxLength": 256},
                        "size": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
            self._chembl_bioactivity,
        )
        self.register(
            OnlineSkillDefinition(
                name="pubmed.search",
                description=(
                    "Search PubMed for biomedical literature by free text and return matching articles with title, "
                    "authors, journal, year, and a PubMed link. Use this to find papers or evidence on a drug, "
                    "target, disease, or method. Returns no results when there is no match."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1, "maxLength": 256},
                        "size": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
            self._search_pubmed,
        )
        self.register(
            OnlineSkillDefinition(
                name="clinicaltrials.search",
                description=(
                    "Search ClinicalTrials.gov for clinical trials by free text (drug name, disease, "
                    "intervention, sponsor) and return matching trials with NCT ID, title, phase, status, "
                    "conditions, and a link. Use this to find clinical evidence or trial status."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1, "maxLength": 256},
                        "size": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
            self._search_clinical_trials,
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

    def _search_uniprot(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        query = _required_string(arguments, "query", label="UniProt search")
        size_int = _resolve_size(arguments, 5)
        url = (
            "https://rest.uniprot.org/uniprotkb/search?query="
            f"{quote(query, safe='')}&format=json"
            "&fields=accession,id,gene_names,protein_name,organism_name,length,reviewed,sequence"
            f"&size={size_int}"
        )
        payload = self._get_json(url)
        raw_results = payload.get("results") if isinstance(payload.get("results"), list) else []
        results: List[Dict[str, Any]] = []
        for entry in raw_results:
            if not isinstance(entry, dict):
                continue
            accession = str(entry.get("primaryAccession") or "").strip().upper()
            if not accession:
                continue
            organism = entry.get("organism") if isinstance(entry.get("organism"), dict) else {}
            genes = entry.get("genes") if isinstance(entry.get("genes"), list) else []
            gene_name = (
                self._nested_text(genes[0], ("geneName", "value"))
                if genes and isinstance(genes[0], dict)
                else ""
            )
            description = entry.get("proteinDescription")
            protein_name = self._nested_text(description, ("recommendedName", "fullName", "value"))
            sequence_data = entry.get("sequence") if isinstance(entry.get("sequence"), dict) else {}
            reviewed = str(entry.get("entryType") or "").lower().startswith("uniprotkb reviewed")
            results.append(
                {
                    "accession": accession,
                    "entryName": str(entry.get("uniProtkbId") or accession).strip(),
                    "geneNames": gene_name,
                    "proteinName": protein_name or gene_name or accession,
                    "organism": str(organism.get("scientificName") or "").strip(),
                    "length": int(sequence_data.get("length") or 0),
                    "sequence": "".join(str(sequence_data.get("value") or "").split()).upper(),
                    "reviewed": reviewed,
                    "sourceUrl": f"https://www.uniprot.org/uniprotkb/{quote(accession, safe='-')}/entry",
                }
            )
        # Rank canonical records first: reviewed (Swiss-Prot) entries before unreviewed
        # fragments, then longer (more complete) sequences. Stable sort preserves the
        # source relevance order within a tier. This is a general ranking principle, not
        # a per-query rule, so the canonical record surfaces even when the source order
        # fluctuates.
        results.sort(key=lambda item: (not item["reviewed"], -int(item.get("length") or 0)))
        return {
            "source": "uniprot",
            "query": query,
            "count": len(results),
            "results": results,
        }

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

    def _search_pubchem(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        identifier = _required_string(arguments, "identifier", label="PubChem search")
        namespace = str(arguments.get("namespace") or "name").strip().lower()
        if namespace not in {"name", "cid", "smiles", "inchi", "inchikey"}:
            namespace = "name"
        property_names = "Title,CanonicalSMILES,IsomericSMILES,MolecularFormula,InChIKey,InChI"
        url = (
            "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/"
            f"{namespace}/{quote(identifier, safe='')}/property/{property_names}/JSON"
        )
        try:
            payload = self._get_json(url)
        except RuntimeError as exc:
            # PubChem answers HTTP 404 when there is no compound for the identifier; report no
            # match honestly instead of raising, so the planner can tell the user nothing was found.
            if str(exc).startswith("HTTP 404"):
                return {
                    "source": "pubchem",
                    "namespace": namespace,
                    "query": identifier,
                    "count": 0,
                    "results": [],
                }
            raise
        table = payload.get("PropertyTable") if isinstance(payload.get("PropertyTable"), dict) else {}
        properties = table.get("Properties") if isinstance(table.get("Properties"), list) else []
        results: List[Dict[str, Any]] = []
        for row in properties:
            if not isinstance(row, dict):
                continue
            smiles = next(
                (
                    str(row.get(key) or "").strip()
                    for key in ("IsomericSMILES", "SMILES", "CanonicalSMILES", "ConnectivitySMILES")
                    if str(row.get(key) or "").strip()
                ),
                "",
            )
            cid = str(row.get("CID") or "").strip()
            if not smiles or not cid:
                continue
            entry: Dict[str, Any] = {
                "cid": cid,
                "title": str(row.get("Title") or f"CID {cid}").strip(),
                "smiles": smiles,
                "sourceUrl": f"https://pubchem.ncbi.nlm.nih.gov/compound/{quote(cid, safe='')}",
            }
            for key, source_key in (
                ("molecularFormula", "MolecularFormula"),
                ("inchiKey", "InChIKey"),
                ("inchi", "InChI"),
            ):
                value = str(row.get(source_key) or "").strip()
                if value:
                    entry[key] = value
            results.append(entry)
        return {
            "source": "pubchem",
            "namespace": namespace,
            "query": identifier,
            "count": len(results),
            "results": results,
        }

    def _rcsb_entry_summary(self, pdb_id: str) -> Dict[str, Any]:
        payload = self._get_json(
            f"https://data.rcsb.org/rest/v1/core/entry/{quote(pdb_id, safe='')}"
        )
        struct = payload.get("struct") if isinstance(payload.get("struct"), dict) else {}
        info = payload.get("rcsb_entry_info") if isinstance(payload.get("rcsb_entry_info"), dict) else {}
        methods = info.get("experimental_methods")
        resolution = info.get("resolution_combined")
        return {
            "pdbId": pdb_id,
            "title": str(struct.get("title") or pdb_id).strip(),
            "method": ", ".join(str(item) for item in methods) if isinstance(methods, list) else str(methods or ""),
            "resolution": (resolution[0] if isinstance(resolution, list) and resolution else None),
            "sourceUrl": f"https://www.rcsb.org/structure/{quote(pdb_id, safe='')}",
            "pdbUrl": f"https://files.rcsb.org/download/{quote(pdb_id, safe='')}.pdb",
            "cifUrl": f"https://files.rcsb.org/download/{quote(pdb_id, safe='')}.cif",
        }

    def _resolve_rcsb(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        identifier = str(arguments.get("identifier") or "").strip().upper()
        summary = self._rcsb_entry_summary(identifier)
        summary.update({"source": "rcsb", "identifier": identifier})
        return summary

    def _search_rcsb(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        text = _required_string(arguments, "text", label="RCSB search")
        size = _resolve_size(arguments, 3)
        search_body = json.dumps(
            {
                "query": {"type": "terminal", "service": "full_text", "parameters": {"value": text}},
                "return_type": "entry",
                "request_options": {"paginate": {"start": 0, "rows": size}},
            }
        )
        response = self._session.post(
            "https://search.rcsb.org/rcsbsearch/v2/query",
            headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "V-Bio-Copilot/1.0"},
            data=search_body,
            timeout=self._timeout_seconds,
        )
        if not response.ok:
            detail = " ".join(str(getattr(response, "text", "") or "").split())[:240]
            raise RuntimeError(f"HTTP {response.status_code}{': ' + detail if detail else ''}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("RCSB search returned a non-object JSON response.")
        result_set = payload.get("result_set") if isinstance(payload.get("result_set"), list) else []
        results: List[Dict[str, Any]] = []
        for hit in result_set:
            if not isinstance(hit, dict):
                continue
            pdb_id = str(hit.get("identifier") or "").strip().upper()
            if not pdb_id:
                continue
            try:
                entry = self._rcsb_entry_summary(pdb_id)
            except RuntimeError:
                continue
            entry["score"] = hit.get("score")
            results.append(entry)
        return {"source": "rcsb", "query": text, "count": len(results), "results": results}

    def _resolve_alphafold(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        accession = _required_string(arguments, "identifier", label="AlphaFold resolve").upper()
        url = f"https://alphafold.ebi.ac.uk/api/prediction/{quote(accession, safe='')}"
        response = self._session.get(
            url,
            headers={"Accept": "application/json", "User-Agent": "V-Bio-Copilot/1.0"},
            timeout=self._timeout_seconds,
        )
        if not response.ok:
            detail = " ".join(str(getattr(response, "text", "") or "").split())[:240]
            raise RuntimeError(f"HTTP {response.status_code}{': ' + detail if detail else ''}")
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise RuntimeError("AlphaFold DB returned an invalid JSON response.") from exc
        entries = payload if isinstance(payload, list) else ([payload] if isinstance(payload, dict) else [])
        entry = next((item for item in entries if isinstance(item, dict) and item.get("pdbUrl")), None)
        if entry is None:
            raise RuntimeError("AlphaFold DB has no predicted structure for that accession.")
        return {
            "source": "alphafold",
            "identifier": accession,
            "accession": str(entry.get("uniprotAccession") or accession),
            "description": str(entry.get("uniprotDescription") or "").strip(),
            "organism": str(entry.get("organismScientificName") or "").strip(),
            "gene": str(entry.get("gene") or "").strip(),
            "modelVersion": str(entry.get("latestVersion") or entry.get("modelVersion") or "").strip(),
            "avgPlddt": entry.get("globalMetricValue"),
            "fractionConfident": entry.get("fractionPlddtConfident"),
            "fractionVeryHigh": entry.get("fractionPlddtVeryHigh"),
            "pdbUrl": str(entry.get("pdbUrl") or ""),
            "cifUrl": str(entry.get("cifUrl") or ""),
            "sourceUrl": f"https://alphafold.ebi.ac.uk/entry/{quote(accession, safe='-')}",
        }

    def _chembl_bioactivity(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        query = _required_string(arguments, "query", label="ChEMBL bioactivity search")
        size = _resolve_size(arguments, 5)
        mol_payload = self._get_json(
            f"https://www.ebi.ac.uk/chembl/api/data/molecule/search.json?q={quote(query)}&limit=1"
        )
        molecules = mol_payload.get("molecules") if isinstance(mol_payload.get("molecules"), list) else []
        if not molecules:
            return {"source": "chembl", "query": query, "count": 0, "results": []}
        compound = molecules[0] if isinstance(molecules[0], dict) else {}
        chembl_id = str(compound.get("molecule_chembl_id") or "").strip()
        if not chembl_id:
            return {"source": "chembl", "query": query, "count": 0, "results": []}
        activity_payload = self._get_json(
            "https://www.ebi.ac.uk/chembl/api/data/activity.json?"
            f"molecule_chembl_id={quote(chembl_id, safe='')}"
            "&standard_type__in=IC50,Ki,Kd,EC50,AC50"
            "&standard_value__isnull=false"
            "&target_pref_name__isnull=false"
            f"&order_by=standard_value&limit={size * 4}"
        )
        activities = activity_payload.get("activities") if isinstance(activity_payload.get("activities"), list) else []
        # Keep the most potent (lowest) measured value per target name.
        best_by_target: Dict[str, Dict[str, Any]] = {}
        for item in activities:
            if not isinstance(item, dict):
                continue
            target = str(item.get("target_pref_name") or "").strip()
            if not target or target.lower() == "unchecked":
                continue
            try:
                value = float(item.get("standard_value"))
            except (TypeError, ValueError):
                continue
            existing = best_by_target.get(target)
            if existing is None or value < existing["value"]:
                best_by_target[target] = {
                    "target": target,
                    "type": str(item.get("standard_type") or "").strip(),
                    "value": value,
                    "units": str(item.get("standard_units") or "nM").strip(),
                    "organism": str(item.get("organism") or "").strip(),
                }
        results = list(best_by_target.values())[:size]
        return {
            "source": "chembl",
            "query": query,
            "compound": {"chemblId": chembl_id, "name": str(compound.get("pref_name") or query).strip()},
            "count": len(results),
            "results": results,
        }

    def _search_pubmed(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        query = _required_string(arguments, "query", label="PubMed search")
        size = _resolve_size(arguments, 5)
        esearch = self._get_json(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
            f"db=pubmed&term={quote(query)}&retmode=json&retmax={size}"
        )
        esearch_result = esearch.get("esearchresult") if isinstance(esearch.get("esearchresult"), dict) else {}
        ids = [str(item) for item in (esearch_result.get("idlist") or []) if str(item).isdigit()]
        if not ids:
            return {"source": "pubmed", "query": query, "count": 0, "results": []}
        esummary = self._get_json(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?"
            f"db=pubmed&id={quote(','.join(ids), safe=',')}&retmode=json"
        )
        summary = esummary.get("result") if isinstance(esummary.get("result"), dict) else {}
        uids = [str(item) for item in (summary.get("uids") or []) if str(item) in ids]
        results: List[Dict[str, Any]] = []
        for uid in uids:
            entry = summary.get(uid) if isinstance(summary.get(uid), dict) else {}
            authors = entry.get("authors") if isinstance(entry.get("authors"), list) else []
            first_author = str(authors[0].get("name") or "").strip() if authors and isinstance(authors[0], dict) else ""
            author_label = (first_author + (" et al." if len(authors) > 1 else "")).strip()
            results.append(
                {
                    "pmid": uid,
                    "title": str(entry.get("title") or "").strip(),
                    "authors": author_label,
                    "journal": str(entry.get("source") or "").strip(),
                    "year": _pubdate_year(entry.get("pubdate")),
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{quote(uid, safe='')}/",
                }
            )
        return {"source": "pubmed", "query": query, "count": len(results), "results": results}

    def _search_clinical_trials(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        query = _required_string(arguments, "query", label="ClinicalTrials search")
        size = _resolve_size(arguments, 5)
        payload = self._get_json(
            f"https://clinicaltrials.gov/api/v2/studies?query.term={quote(query)}&pageSize={size}&format=json"
        )
        studies = payload.get("studies") if isinstance(payload.get("studies"), list) else []
        results: List[Dict[str, Any]] = []
        for study in studies:
            if not isinstance(study, dict):
                continue
            proto = study.get("protocolSection") if isinstance(study.get("protocolSection"), dict) else {}
            ident = proto.get("identificationModule") if isinstance(proto.get("identificationModule"), dict) else {}
            nct_id = str(ident.get("nctId") or "").strip()
            if not nct_id:
                continue
            status_mod = proto.get("statusModule") if isinstance(proto.get("statusModule"), dict) else {}
            design = proto.get("designModule") if isinstance(proto.get("designModule"), dict) else {}
            cond_mod = proto.get("conditionsModule") if isinstance(proto.get("conditionsModule"), dict) else {}
            phases = design.get("phases") if isinstance(design.get("phases"), list) else []
            conditions = cond_mod.get("conditions") if isinstance(cond_mod.get("conditions"), list) else []
            results.append(
                {
                    "nctId": nct_id,
                    "title": str(ident.get("briefTitle") or "").strip(),
                    "status": str(status_mod.get("overallStatus") or "").strip(),
                    "phase": ", ".join(str(ph) for ph in phases) if phases else "",
                    "conditions": [str(c) for c in conditions[:3]],
                    "url": f"https://clinicaltrials.gov/study/{quote(nct_id, safe='')}",
                }
            )
        return {"source": "clinicaltrials", "query": query, "count": len(results), "results": results}
