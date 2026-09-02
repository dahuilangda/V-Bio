from __future__ import annotations

import copy
import email.utils
import json
from concurrent.futures import ThreadPoolExecutor
import os
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote, urlparse

import requests


# Per-host minimum spacing between requests, to respect documented upstream rate limits.
# The reservation is keyed by host so unrelated sources are not serialized. Values come from the
# services' own usage policies (researched 2026-08 against the official docs):
#   www.ebi.ac.uk          ChEMBL data API — no more than 1 req/s without an API key (official docs).
#   eutils.ncbi.nlm.nih.gov NCBI E-utilities — more than 3 req/s without an API key receives an
#                          error message (NBK25497); with api_key the limit is 10 req/s.
#   pubchem.ncbi.nlm.nih.gov PubChem PUG REST — "not make more than 5 requests per second"
#                          (official programmatic-access policy); exceeding returns HTTP 503.
#   rest.uniprot.org        UniProt — no numeric limit published; spaced for politeness.
#   alphafold.ebi.ac.uk     AlphaFold DB — no documented limit; EBI fair-use spacing.
#   data/search.rcsb.org    RCSB — "a handful of requests per second" recommended (official docs).
#   clinicaltrials.gov      ClinicalTrials.gov v2 — no documented limit; polite spacing.
_HOST_RATE_SECONDS: Dict[str, float] = {
    "www.ebi.ac.uk": 1.0,
    "eutils.ncbi.nlm.nih.gov": 0.34,
    # Spacings re-derived from each source's published policy with safety margin:
    # PubChem hard-asks ≤5 req/s → 0.25s (≤4 req/s); RCSB "a handful per second" → 0.25s;
    # UniProt publishes no fixed figure but restricts parallelism → 0.25s serial.
    "pubchem.ncbi.nlm.nih.gov": 0.25,
    "rest.uniprot.org": 0.25,
    "alphafold.ebi.ac.uk": 0.34,
    "data.rcsb.org": 0.25,
    "search.rcsb.org": 0.25,
    "clinicaltrials.gov": 0.25,
}
_DEFAULT_RATE_SECONDS = 0.0
# NCBI with an API key: 10 req/s allowed, so the spacing relaxes below the no-key interval.
_NCBI_KEYED_RATE_SECONDS = 0.11


@dataclass(frozen=True)
class OnlineSkillDefinition:
    name: str
    description: str
    input_schema: Dict[str, Any]
    # Input-language boundary: the online databases are English-indexed, so a read skill rejects
    # non-English query text by default (the harness audits it before execution). A skill whose
    # purpose is handling non-English input — terminology conversion — declares this True.
    accepts_non_english_input: bool = False


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


def _is_http_status(exc: Exception, status: int) -> bool:
    """Return whether a fetch RuntimeError reports the given HTTP status.

    ``_fetch_json`` raises ``RuntimeError("HTTP <status>...")`` for non-2xx responses. Status-code
    detection is matched on the stable message prefix so callers can distinguish an authoritative
    "no such record" (HTTP 404) from a transport/source failure (5xx, timeouts, connection drops) —
    the two must map to NO_MATCH and FAILED respectively, never conflated.
    """
    return str(exc).startswith(f"HTTP {status}")


class OnlineDatabaseSkills:
    """Registry of read-only, atomic online-database skills."""

    def __init__(
        self,
        *,
        session: requests.Session,
        timeout_seconds: float = 8.0,
        cache_ttl_seconds: float = 600.0,
        contact_email: str = "",
        ncbi_email: str = "",
        ncbi_api_key: str = "",
    ) -> None:
        self._session = session
        self._timeout_seconds = max(1.0, min(30.0, float(timeout_seconds)))
        self._cache_ttl_seconds = max(1.0, min(3600.0, float(cache_ttl_seconds)))
        self._cache: Dict[str, tuple[float, Dict[str, Any]]] = {}
        self._cache_lock = threading.Lock()
        self._host_next_allowed: Dict[str, float] = {}
        self._rate_lock = threading.Lock()
        self._definitions: Dict[str, OnlineSkillDefinition] = {}
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {}
        # Per-call outbound proxy (from runtime settings). None means direct connection.
        self._proxies: Optional[Dict[str, str]] = None
        # Identification the upstreams themselves ask for. UniProt's programmatic-access help
        # asks for a contact email inside the User-Agent so they can reach the operator before
        # blocking; NCBI's E-utilities guidelines (NBK25497) ask for `tool` and `email` on every
        # request for exactly the same reason, and `api_key` lifts the 3 req/s cap to 10 req/s.
        self._contact_email = (
            contact_email.strip()
            or os.environ.get("VBIO_COPILOT_CONTACT_EMAIL", "").strip()
        )
        self._ncbi_email = (
            ncbi_email.strip()
            or os.environ.get("VBIO_COPILOT_NCBI_EMAIL", "").strip()
            or self._contact_email
        )
        self._ncbi_api_key = (
            ncbi_api_key.strip()
            or os.environ.get("VBIO_COPILOT_NCBI_API_KEY", "").strip()
        )
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
            "properties": {"identifier": {"type": "string", "minLength": 1, "maxLength": 512}},
            "required": ["identifier"],
            "additionalProperties": False,
        }
        self.register(
            OnlineSkillDefinition(
                name="uniprot.search",
                description=(
                    "Search UniProtKB and return ranked candidate entries with accession, gene name, protein name, "
                    "organism, and sequence. Use UniProt field syntax for precision — the valid field names are "
                    "gene, protein_name, organism_name, organism_id (a numeric taxonomy id), accession, and "
                    "reviewed; combine fields with AND. Any other field name makes the source reject the whole "
                    "query with an error. Add an explicit organism filter when the entity exists in multiple "
                    "species, and request reviewed entries when curated data is required. An unstated organism "
                    "is an unresolved choice, never a default: query without the organism filter and resolve "
                    "the organism — and the isoform, when the gene names a family — through the user's choice; "
                    "every candidate you present must state its organism and isoform from the record. Never send a bare gene "
                    "name or protein name without field syntax — it returns irrelevant matches across all organisms. "
                    "BOUNDARY: UniProt returns SEQUENCES and protein metadata. Choose it when the consuming field "
                    "needs an amino-acid sequence; when the consuming field needs a 3D structure, use rcsb.search "
                    "(experimental) or alphafold.resolve (predicted) instead. The query must be in English — a "
                    "query in another language returns irrelevant or no matches; convert it with translate.to_english "
                    "first."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1, "maxLength": 512},
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
                description=(
                    "Resolve one UniProt accession or entry name to its authoritative protein sequence and "
                    "metadata, with the record's organism and gene name stated. The accession must already be "
                    "pinned to one organism and isoform — resolve an ambiguous gene with uniprot.search first "
                    "and let the user choose. Verify the returned organism against the user's intent before "
                    "consuming the sequence. Returns no results when the record does not exist. BOUNDARY: "
                    "returns SEQUENCES and protein metadata; a consuming field that needs a 3D structure wants "
                    "rcsb.search (experimental) or alphafold.resolve (predicted) instead."
                ),
                input_schema=identifier_schema,
            ),
            self._resolve_uniprot,
        )
        self.register(
            OnlineSkillDefinition(
                name="pubchem.search",
                description=(
                    "Look up one PubChem compound and return its SMILES, molecular formula, InChIKey, and InChI. "
                    "Choose the namespace that matches the identifier: 'name' for a compound name, trade name, "
                    "synonym, or registry number (vendor / CAS / internal compound codes resolve as names); 'cid' "
                    "for a numeric PubChem CID; 'smiles' for a SMILES string; 'inchi' for an InChI string; "
                    "'inchikey' for an InChIKey. Defaults to 'name'. Returns no results when there is no "
                    "authoritative match. BOUNDARY: use this to resolve a molecule the user referred to by "
                    "anything other than a SMILES string. When the user already provides a SMILES, no lookup is "
                    "needed — pass the SMILES to the consuming field directly. Never read a compound's identity, "
                    "correctness, or properties out of a SMILES string — inference from SMILES is unreliable; a "
                    "SMILES is an authoritative key only when this lookup (or the user) produced it, so never "
                    "state or imply one is 'ready to use' before the lookup has run. A compound's SMILES is a "
                    "retrieved value, never recalled from memory: writing one from model knowledge — in a "
                    "message, a question, or an operation — is fabrication; resolve the compound here first. "
                    "The identifier must be in "
                    "English (or a registry number): a name in another language returns no match — convert it "
                    "with translate.to_english first."
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
                description=(
                    "Resolve one RCSB PDB identifier to authoritative entry metadata (title with organism, "
                    "method, resolution) and structure file links. The entry's title states its organism and "
                    "the protein it contains — verify both against the user's intent before consuming the "
                    "structure. Returns no results when the id does not exist. BOUNDARY: resolves an EXACT id "
                    "only; find structures by name with rcsb.search."
                ),
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
                    "links. Use this to FIND structures by name; use rcsb.resolve only for an exact PDB id. Put "
                    "the organism into the search text alongside the protein name so the correct species' entries "
                    "rank first; an unstated organism is an unresolved choice, never a default: search without "
                    "it and resolve the organism — and the isoform, when the name is a gene family — through "
                    "the user's choice among the returned entries: every entry you offer must state its "
                    "organism and isoform from its title. Never assume an unstated organism. Returns no results when there is no match. BOUNDARY: RCSB returns experimental "
                    "3D STRUCTURE entries — choose it when the consuming field needs a structure file (a "
                    "receptor / target structure, a template); when the consuming field needs only an amino-acid "
                    "sequence, use uniprot instead. When several entries match, never select one silently: ask "
                    "the user a choice question listing the entries (pdb id, title, method, resolution) and use "
                    "only the entry the user picks. The search text must be in English; convert a non-English "
                    "name with translate.to_english first."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "minLength": 1, "maxLength": 512},
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
                    "(pLDDT), organism and gene, and PDB / mmCIF download links. Use a UniProt accession, not "
                    "a gene name — the accession must already be pinned to one organism and isoform (resolve "
                    "an ambiguous gene with uniprot.search first and let the user choose), and the returned "
                    "record's organism must be verified against the user's intent before the model is used. "
                    "BOUNDARY: this returns a PREDICTED monomer model, not an experimental structure — choose it "
                    "when the user accepts a predicted model, or when no experimental structure exists for the "
                    "protein; prefer rcsb.search when an experimental structure is required."
                ),
                input_schema=identifier_schema,
            ),
            self._resolve_alphafold,
        )
        self.register(
            OnlineSkillDefinition(
                name="chembl.bioactivity",
                description=(
                    "Compound-directed bioactivity: given a COMPOUND (drug name / ChEMBL molecule), return the "
                    "TARGETS it hits ranked by measured potency (IC50 / Ki / Kd / EC50). The resolver matches "
                    "the name to ChEMBL's best-ranked molecule and echoes its identity in 'compound' — verify "
                    "that identity (name, ChEMBL id) against the user's intent before using the activities; "
                    "when it differs, resolve the compound with pubchem.search or restate the query. For the "
                    "inverse direction — find compounds for a target — use chembl.target_activity instead. "
                    "Returns no results when the compound or its activity is unknown. The query must be in "
                    "English — convert a non-English name with translate.to_english first."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1, "maxLength": 512},
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
                name="chembl.target_activity",
                description=(
                    "Target-directed bioactivity: find known active compounds (inhibitors / ligands) for a "
                    "biological TARGET and return them ranked by measured potency (IC50 / Ki / Kd / EC50), each "
                    "with name and canonical SMILES. Resolve the target one of three ways, most precise first: "
                    "(1) a UniProt accession in 'accession' — resolve a gene symbol to its accession with "
                    "uniprot.search first, then pass that accession here; (2) a ChEMBL target id in "
                    "'target_chembl_id'; (3) a free-text protein name in 'query' alone, matched on ChEMBL's "
                    "preferred name (gene symbols rarely match — prefer the accession path). An unstated "
                    "organism is an unresolved choice, never a default: the resolver ranks candidates by the "
                    "'organism' you pass and returns its best match with the target's organism stated — verify "
                    "the returned organism (and isoform, when the gene names a family) against the user's "
                    "intent before using the activities. Returns no results when the target or its activity "
                    "is unknown. The "
                    "query and organism must be in English — convert a non-English name with "
                    "translate.to_english first."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 512,
                            "description": "The user-facing target label (name or gene symbol). Echoed in the result.",
                        },
                        "accession": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 64,
                            "description": "UniProt accession. Most precise target key.",
                        },
                        "target_chembl_id": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 64,
                            "description": "A ChEMBL target id, used directly when already known.",
                        },
                        "organism": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 128,
                            "description": "Organism scientific name used to rank matching targets; unstated is allowed — the best match is returned with its organism stated, for verification against the user's intent.",
                        },
                        "activity_type": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 128,
                            "description": "Comma-separated ChEMBL standard_type values (default IC50,Ki,Kd,EC50,AC50).",
                        },
                        "size": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
            self._chembl_target_activity,
        )
        self.register(
            OnlineSkillDefinition(
                name="pubmed.search",
                description=(
                    "Search PubMed for biomedical literature by free text and return matching articles with title, "
                    "first author, journal, year, and a PubMed link. Use this to find papers or evidence on a drug, "
                    "target, disease, or method. Returns no results when there is no match. The query "
                    "must be in English — convert a non-English term with translate.to_english first."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1, "maxLength": 512},
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
                    "conditions (first three listed), and a link. Use this to find clinical evidence or trial status. The "
                    "query must be in English — convert a non-English term with translate.to_english first."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1, "maxLength": 512},
                        "size": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
            self._search_clinical_trials,
        )

    # A descriptive User-Agent is requested by EBI/NCBI for programmatic traffic and helps
    # their fair-use throttling distinguish legitimate research clients. UniProt's help pages
    # additionally ask for a contact email INSIDE the User-Agent so they can reach the operator
    # before blocking, so the email is appended when configured.
    def _http_user_agent(self) -> str:
        agent = "V-Bio-Copilot/1.1 (read-only research lookups"
        if self._contact_email:
            agent += f"; contact: {self._contact_email}"
        return agent + ")"

    # Transient-failure retry, mirroring the reliability patterns the upstreams themselves
    # publish: UniProt's official examples use urllib3 Retry over [500, 502, 503, 504]; the
    # official chembl_webresource_client retries 400–420 and 500–504 (three total retries,
    # backoff factor 2) because ChEMBL's Apache proxy intermittently 502s (chembl issues
    # #28/#29/#31); RCSB documents HTTP 429 for rate limiting and recommends exponential
    # backoff. Retry-After (RFC 9110: delta-seconds or HTTP-date) is honored on 429/503 the
    # way urllib3's RETRY_AFTER_STATUS_CODES does, capped so a copilot turn never hangs on a
    # server-demanded wait. 4xx other than 408/429 is deterministic (404 = no authoritative
    # match) and must NOT be retried, so per-skill honest-empty handling stays intact.
    _RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
    _RETRY_MAX_ATTEMPTS = 4
    _RETRY_BACKOFF_SECONDS = 0.4
    _RETRY_BACKOFF_FACTOR = 2.5
    _RETRY_BACKOFF_CAP_SECONDS = 8.0
    _RETRY_AFTER_CAP_SECONDS = 8.0
    # NCBI E-utilities identification (NBK25497): `tool` must uniquely identify the software
    # with no spaces; `email` must be the developer's address; `api_key` lifts 3 req/s → 10 req/s.
    _NCBI_TOOL = "vbio-copilot"

    def _ncbi_ident_params(self) -> str:
        """URL fragment carrying NCBI's requested tool/email/api_key identification."""
        parts = [f"tool={quote(self._NCBI_TOOL, safe='')}"]
        if self._ncbi_email:
            parts.append(f"email={quote(self._ncbi_email, safe='')}")
        if self._ncbi_api_key:
            parts.append(f"api_key={quote(self._ncbi_api_key, safe='')}")
        return "&".join(parts)

    def _host_rate_interval(self, host: str) -> float:
        if host == "eutils.ncbi.nlm.nih.gov" and self._ncbi_api_key:
            return _NCBI_KEYED_RATE_SECONDS
        return _HOST_RATE_SECONDS.get(host, _DEFAULT_RATE_SECONDS)

    def _wait_for_host_rate(self, url: str) -> None:
        """Space requests to a host per its documented rate limit (reservation pattern).

        ChEMBL allows <=1 req/s without an API key; NCBI errors above 3 req/s without a key;
        PubChem asks for <=5 req/s. Under the lock we reserve the next allowed slot for this
        host (slot = max(now, previous_slot) + interval), release the lock, then sleep until
        the slot. Concurrent requests to the SAME host therefore queue at the rate limit,
        while requests to a different host are not blocked by this one.
        """
        host = (urlparse(url).hostname or "").lower()
        interval = self._host_rate_interval(host)
        if interval <= 0:
            return
        with self._rate_lock:
            now = time.monotonic()
            previous = self._host_next_allowed.get(host, 0.0)
            slot = max(now, previous)
            self._host_next_allowed[host] = slot + interval
            sleep_for = slot - now
        if sleep_for > 0:
            time.sleep(sleep_for)

    @staticmethod
    def _parse_retry_after(value: Any) -> Optional[float]:
        """Parse a Retry-After header (delta-seconds or HTTP-date) to a non-negative delay."""
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return max(0.0, float(text))
        except ValueError:
            pass
        try:
            parsed = email.utils.parsedate_tz(text)
            if parsed is None:
                return None
            when = email.utils.mktime_tz(parsed)
            return max(0.0, when - time.time())
        except (TypeError, ValueError, OverflowError, OSError):
            return None

    def _retry_after_seconds(self, response: Any) -> Optional[float]:
        """The server-demanded wait for this response, capped to the turn's patience."""
        headers = getattr(response, "headers", None)
        if not hasattr(headers, "get"):
            return None
        delay = self._parse_retry_after(headers.get("Retry-After"))
        if delay is None:
            return None
        return min(delay, self._RETRY_AFTER_CAP_SECONDS)

    def _sleep_backoff(self, backoff: float) -> None:
        """Equal-jitter exponential backoff (AWS "Exponential Backoff and Jitter").

        Half the computed delay fixed, half random — sleeps grow exponentially while avoiding
        the synchronized retry bursts that plain exponential backoff produces.
        """
        time.sleep(backoff * 0.5 + random.uniform(0.0, backoff * 0.5))

    def _request_with_retry(self, method: str, url: str, *, data: str | None = None) -> Any:
        """Issue one HTTP request, retrying transient failures with bounded jittered backoff.

        Retries connection errors, timeouts, and the retryable statuses (408/429/5xx) up to
        ``_RETRY_MAX_ATTEMPTS``, honoring a capped Retry-After when the server sends one, and
        returns the final response (success or the last failure). Raises ``RuntimeError`` only
        when the source stays unreachable after every attempt, so the caller can surface an
        honest "source unavailable" signal instead of a raw transport exception.
        """
        headers = {"Accept": "application/json", "User-Agent": self._http_user_agent()}
        if method != "GET":
            headers["Content-Type"] = "application/json"
        # Separate connect/read timeouts: a 5s connect window sits just above the TCP
        # retransmission multiple requests recommends; the read window is the configured one.
        timeout = (5.0, self._timeout_seconds)
        backoff = self._RETRY_BACKOFF_SECONDS
        response: Any = None
        for attempt in range(self._RETRY_MAX_ATTEMPTS):
            self._wait_for_host_rate(url)
            try:
                if method == "GET":
                    response = self._session.get(url, headers=headers, timeout=timeout, proxies=self._proxies)
                else:
                    response = self._session.post(url, headers=headers, data=data, timeout=timeout, proxies=self._proxies)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                if attempt + 1 == self._RETRY_MAX_ATTEMPTS:
                    raise RuntimeError(f"online source unreachable: {exc}") from exc
                self._sleep_backoff(backoff)
                backoff = min(backoff * self._RETRY_BACKOFF_FACTOR, self._RETRY_BACKOFF_CAP_SECONDS)
                continue
            status = int(getattr(response, "status_code", 0) or 0)
            if status in self._RETRYABLE_STATUS and attempt + 1 < self._RETRY_MAX_ATTEMPTS:
                # A server-demanded wait (Retry-After on 429/503) overrides computed backoff.
                wait = self._retry_after_seconds(response) if status in (429, 503) else None
                if wait is not None:
                    time.sleep(wait)
                else:
                    self._sleep_backoff(backoff)
                backoff = min(backoff * self._RETRY_BACKOFF_FACTOR, self._RETRY_BACKOFF_CAP_SECONDS)
                continue
            return response
        return response

    def _get_json(self, url: str) -> Dict[str, Any]:
        return self._fetch_json("GET", url)

    def _post_json(self, url: str, body: str) -> Dict[str, Any]:
        return self._fetch_json("POST", url, data=body)

    def _fetch_json(self, method: str, url: str, *, data: str | None = None) -> Dict[str, Any]:
        response = self._request_with_retry(method, url, data=data)
        if not response.ok:
            detail = " ".join(str(getattr(response, "text", "") or "").split())[:240]
            suffix = f": {detail}" if detail else ""
            status = int(getattr(response, "status_code", 0) or 0)
            # 4xx (except 408 request-timeout and 429 rate-limit, both retryable) is a DETERMINISTIC
            # rejection: the source evaluated the request and refused it as invalid. The error text
            # must say so — the planner reads it and must fix the arguments (query syntax, field
            # names, identifier format) and retry. Labeling this class "source unavailable" would
            # send the user down a retry-later path that can never succeed while the request is
            # malformed. 404 stays a plain status so call-site _is_http_status(404) no-match
            # handling keeps working.
            if 400 <= status < 500 and status not in (404, 408, 429):
                raise RuntimeError(
                    f"HTTP {status}{suffix} — the source rejected the request as invalid. This is not an "
                    "outage: correct the request arguments (query syntax, field names, identifier format) "
                    "and retry under a new operation id."
                )
            raise RuntimeError(f"HTTP {status}{suffix}")
        if int(getattr(response, "status_code", 0) or 0) == 204:
            # 204 No Content is an authoritative empty answer (RCSB's search API returns it for a
            # query with zero hits). Map it to the empty-object shape so search handlers report
            # NO_MATCH instead of tripping the non-JSON guard below as a source failure.
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            # HTTP 200 but a non-JSON body (an empty response or an HTML error page) — some
            # upstreams misbehave this way under load. Report it as a source failure so the harness
            # surfaces SOURCE UNAVAILABLE, instead of letting a raw JSONDecodeError propagate.
            preview = " ".join(str(getattr(response, "text", "") or "").split())[:120]
            raise RuntimeError(
                f"source returned a non-JSON response{f': {preview}' if preview else ' (empty body)'}"
            ) from exc
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
        try:
            payload = self._get_json(url)
        except RuntimeError as exc:
            if _is_http_status(exc, 404):
                # UniProt answers 404 for a nonexistent accession — an authoritative "no such
                # record", NOT a source failure. Return the empty shape so the harness classifies
                # it NO_MATCH and the planner tells the user the record does not exist (never
                # "source unavailable").
                return {"source": "uniprot", "identifier": identifier, "count": 0, "results": []}
            raise
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
        organism_payload = payload.get("organism") if isinstance(payload.get("organism"), dict) else {}
        organism = self._nested_text(organism_payload, ("scientificName",))
        return {
            "source": "uniprot",
            "identifier": accession,
            "sourceUrl": f"https://www.uniprot.org/uniprotkb/{quote(accession, safe='-')}/entry",
            "accession": accession,
            "organism": organism,
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

    @staticmethod
    def _format_rcsb_entry(payload: Dict[str, Any], pdb_id: str) -> Dict[str, Any]:
        struct = payload.get("struct") if isinstance(payload.get("struct"), dict) else {}
        info = payload.get("rcsb_entry_info") if isinstance(payload.get("rcsb_entry_info"), dict) else {}
        # The Data API reports the method as the singular string "experimental_method"; older
        # payloads carried a plural list. Accept both so the method never silently disappears
        # from an entry the user must choose between.
        methods = info.get("experimental_methods")
        if not isinstance(methods, list) or not methods:
            singular = str(info.get("experimental_method") or "").strip()
            methods = [singular] if singular else []
        resolution = info.get("resolution_combined")
        return {
            "pdbId": pdb_id,
            "title": str(struct.get("title") or pdb_id).strip(),
            "method": ", ".join(str(item) for item in methods if str(item or "").strip()),
            "resolution": (resolution[0] if isinstance(resolution, list) and resolution else None),
            "sourceUrl": f"https://www.rcsb.org/structure/{quote(pdb_id, safe='')}",
            # Advertise ONLY the mmCIF link: mmCIF is RCSB's master archive format and exists
            # for EVERY entry, while the legacy PDB-format file exists only for some (large or
            # complex entries answer 404 on .pdb). A record field is a contract — handing the
            # planner a link that may not exist is the root cause of dead downloads, so the
            # dead one is simply not exposed.
            "cifUrl": f"https://files.rcsb.org/download/{quote(pdb_id, safe='')}.cif",
        }

    def _rcsb_entry_summary(self, pdb_id: str) -> Dict[str, Any]:
        payload = self._get_json(
            f"https://data.rcsb.org/rest/v1/core/entry/{quote(pdb_id, safe='')}"
        )
        return self._format_rcsb_entry(payload, pdb_id)

    def _rcsb_entry_summaries(self, pdb_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Fetch per-entry summaries for the hit ids.

        Entries are independent GETs to data.rcsb.org — fetched through a small bounded pool
        instead of a serial loop (a size-10 search paid 10 sequential round trips, the largest
        single-skill stall on the lookup path). The per-host rate limiter still bounds the
        outbound rate; failures mark the entry dropped exactly as the serial path did.
        """
        """Fetch entry summaries one id at a time.

        The core/entry endpoint accepts exactly ONE entry id (comma-separated ids 404 —
        batching is documented only for holdings endpoints), so a search with N hits costs N
        summary requests. Kept as a method for the per-entry failure tolerance the caller
        relies on.
        """
        unique_ids: List[str] = []
        seen: set[str] = set()
        for pdb_id in pdb_ids:
            normalized = str(pdb_id or "").strip().upper()
            if normalized and normalized not in seen:
                seen.add(normalized)
                unique_ids.append(normalized)
        if not unique_ids:
            return {}
        def _fetch_one(one_id: str) -> tuple[str, Dict[str, Any] | None]:
            # Per-entry tolerance: retries that exhaust raise here — drop that one hit,
            # never the whole search (the caller marks the result incomplete).
            try:
                response = self._request_with_retry(
                    "GET", f"https://data.rcsb.org/rest/v1/core/entry/{quote(one_id, safe='')}"
                )
                if not response.ok:
                    return one_id, None
                return one_id, self._format_rcsb_entry(response.json(), one_id)
            except (RuntimeError, TypeError, ValueError):
                return one_id, None

        summaries: Dict[str, Dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            for one_id, formatted in pool.map(_fetch_one, unique_ids):
                if formatted is not None:
                    summaries[one_id] = formatted
        return summaries

    def _resolve_rcsb(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        identifier = str(arguments.get("identifier") or "").strip().upper()
        try:
            summary = self._rcsb_entry_summary(identifier)
        except RuntimeError as exc:
            if _is_http_status(exc, 404):
                # RCSB answers 404 for an unknown PDB id — authoritative "no such entry" (NO_MATCH),
                # not a source failure.
                return {"source": "rcsb", "identifier": identifier, "count": 0, "results": []}
            raise
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
        payload = self._post_json(
            "https://search.rcsb.org/rcsbsearch/v2/query", search_body
        )
        result_set = payload.get("result_set") if isinstance(payload.get("result_set"), list) else []
        pdb_ids = [
            str(hit.get("identifier") or "").strip().upper()
            for hit in result_set
            if isinstance(hit, dict) and str(hit.get("identifier") or "").strip()
        ]
        # Per-entry summary fetch with tolerance (see _rcsb_entry_summaries): a hit whose
        # summary exhausts its retries is dropped and REPORTED, never fetched twice.
        summaries = self._rcsb_entry_summaries(pdb_ids)
        results: List[Dict[str, Any]] = []
        dropped: List[str] = []
        for hit in result_set:
            if not isinstance(hit, dict):
                continue
            pdb_id = str(hit.get("identifier") or "").strip().upper()
            if not pdb_id:
                continue
            entry = summaries.get(pdb_id)
            if entry is None:
                dropped.append(pdb_id)
                continue
            entry["score"] = hit.get("score")
            results.append(entry)
        payload = {"source": "rcsb", "query": text, "count": len(results), "results": results}
        if dropped:
            # A hit whose entry summary could not be fetched is data the planner must know
            # vanished — report it instead of a silently shorter result set.
            payload["incomplete"] = True
            payload["droppedIds"] = dropped
        return payload

    def _resolve_alphafold(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        accession = _required_string(arguments, "identifier", label="AlphaFold resolve").upper()
        url = f"https://alphafold.ebi.ac.uk/api/prediction/{quote(accession, safe='')}"
        response = self._request_with_retry("GET", url)
        if not response.ok:
            if int(getattr(response, "status_code", 0)) == 404:
                # AlphaFold answers 404 when no predicted structure exists for the accession —
                # authoritative "no such prediction" (NO_MATCH), not a source failure.
                return {
                    "source": "alphafold",
                    "identifier": accession,
                    "count": 0,
                    "results": [],
                }
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
            units = str(item.get("standard_units") or "nM").strip()
            rank = self._potency_rank_nm(value, units)
            existing = best_by_target.get(target)
            if existing is None or rank < existing["_rank"]:
                best_by_target[target] = {
                    "target": target,
                    "activityType": str(item.get("standard_type") or "").strip(),
                    "value": value,
                    "units": units,
                    "organism": str(item.get("organism") or "").strip(),
                    "_rank": rank,
                }
        results = sorted(best_by_target.values(), key=lambda row: row["_rank"])[:size]
        for row in results:
            row.pop("_rank", None)
        return {
            "source": "chembl",
            "query": query,
            "compound": {"chemblId": chembl_id, "name": str(compound.get("pref_name") or query).strip()},
            "count": len(results),
            "results": results,
        }

    # ------------------------------------------------------------------ #
    # Target-directed bioactivity (inverse of _chembl_bioactivity).
    # Canonical ChEMBL "target report card" join, all on the stable filter
    # API (target -> activity -> molecule), the same chain TeachOpenCADD
    # T001 teaches: resolve target_chembl_id, pull activities ordered by
    # potency, then hydrate the molecules for canonical SMILES.
    # ------------------------------------------------------------------ #

    _CHEMBL_DATA_API = "https://www.ebi.ac.uk/chembl/api/data"

    @staticmethod
    def _potency_rank_nm(value: float, units: str) -> float:
        """Normalize a measured potency to nanomolar for RANKING only (the record keeps the
        original value+units). Non-molar units (ug.mL-1, %, ratio) rank last — comparing their
        raw number against nM values is meaningless, which is exactly the bug this fixes."""
        unit = (units or "").strip().lower()
        if unit in ("nm", "nanomolar"):
            return value
        if unit in ("um", "µm", "micromolar"):
            return value * 1000.0
        if unit in ("pm", "picomolar"):
            return value / 1000.0
        if unit in ("mm", "millimolar"):
            return value * 1_000_000.0
        return float("inf")

    def _resolve_chembl_target(
        self,
        *,
        query: str,
        accession: str | None,
        target_chembl_id: str | None,
        organism: str | None,
    ) -> Dict[str, Any] | None:
        """Resolve a target to one ChEMBL target record.

        Precision order: an explicit ``target_chembl_id`` (direct lookup), then a UniProt
        ``accession`` (``target_components__accession`` — the most reliable cross-link), then a
        free-text ``query`` matched on preferred name. When several targets match, prefer the
        requested organism and the most specific (single-protein) record — a general ranking
        principle, not a per-query rule.
        """
        if accession:
            payload = self._get_json(
                f"{self._CHEMBL_DATA_API}/target.json?"
                f"target_components__accession={quote(accession, safe='')}&limit=25"
            )
            raw_targets = payload.get("targets") if isinstance(payload.get("targets"), list) else []
        elif target_chembl_id:
            # A direct id lookup that 404s is ChEMBL answering "no such target id" — an
            # authoritative NO_MATCH, not a source failure (every other resolve path does the
            # same); surface it as an empty result instead of letting HTTP 404 become FAILED.
            try:
                payload = self._get_json(
                    f"{self._CHEMBL_DATA_API}/target/{quote(target_chembl_id, safe='')}.json"
                )
            except RuntimeError as exc:
                if "HTTP 404" in str(exc):
                    return []
                raise
            raw_targets = [payload] if isinstance(payload, dict) and payload.get("target_chembl_id") else []
        else:
            payload = self._get_json(
                f"{self._CHEMBL_DATA_API}/target.json?"
                f"pref_name__icontains={quote(query, safe='')}&limit=25"
            )
            raw_targets = payload.get("targets") if isinstance(payload.get("targets"), list) else []

        candidates: List[Dict[str, Any]] = []
        for raw in raw_targets:
            if not isinstance(raw, dict):
                continue
            chembl_id = str(raw.get("target_chembl_id") or "").strip()
            if not chembl_id:
                continue
            target_organism = str(raw.get("organism") or "").strip()
            target_type = str(raw.get("target_type") or "").strip()
            pref_name = str(raw.get("pref_name") or "").strip()
            candidates.append(
                {
                    "chemblId": chembl_id,
                    "name": pref_name or chembl_id,
                    "organism": target_organism,
                    "type": target_type,
                    # rank-only fields stripped before returning
                    "_organism_mismatch": bool(organism) and target_organism.lower() != organism.lower(),
                    "_not_single_protein": target_type.upper() != "SINGLE PROTEIN",
                }
            )
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item["_organism_mismatch"], item["_not_single_protein"]))
        chosen = candidates[0]
        # The resolver picks its best-ranked match: surface HOW MANY candidates existed so
        # the choice is visible to the planner (it must verify organism/isoform per the
        # contract) instead of a silent single-result illusion.
        return {
            key: value for key, value in chosen.items() if not key.startswith("_")
        } | {"matchedTargets": len(candidates)}

    def _hydrate_chembl_molecules(self, molecule_ids: List[str]) -> Dict[str, Dict[str, str]]:
        """Fetch canonical SMILES + preferred name for a batch of ChEMBL molecule ids."""
        if not molecule_ids:
            return {}
        ids_param = quote(",".join(molecule_ids), safe="")
        payload = self._get_json(
            f"{self._CHEMBL_DATA_API}/molecule.json?"
            f"molecule_chembl_id__in={ids_param}&limit={len(molecule_ids)}"
        )
        molecules = payload.get("molecules") if isinstance(payload.get("molecules"), list) else []
        hydrated: Dict[str, Dict[str, str]] = {}
        for raw in molecules:
            if not isinstance(raw, dict):
                continue
            chembl_id = str(raw.get("molecule_chembl_id") or "").strip()
            if not chembl_id:
                continue
            structures = raw.get("molecule_structures") if isinstance(raw.get("molecule_structures"), dict) else {}
            hydrated[chembl_id] = {
                "name": str(raw.get("pref_name") or "").strip(),
                "smiles": str(structures.get("canonical_smiles") or "").strip(),
            }
        return hydrated

    def _chembl_target_activity(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        query = _required_string(arguments, "query", label="ChEMBL target activity search")
        size = _resolve_size(arguments, 5)
        accession = str(arguments.get("accession") or "").strip().upper() or None
        target_chembl_id = str(arguments.get("target_chembl_id") or "").strip().upper() or None
        organism = str(arguments.get("organism") or "").strip() or None
        activity_type = str(arguments.get("activity_type") or "IC50,Ki,Kd,EC50,AC50").strip() or "IC50,Ki,Kd,EC50,AC50"

        target = self._resolve_chembl_target(
            query=query, accession=accession, target_chembl_id=target_chembl_id, organism=organism
        )
        if target is None:
            return {"source": "chembl", "query": query, "count": 0, "results": []}

        activity_payload = self._get_json(
            f"{self._CHEMBL_DATA_API}/activity.json?"
            f"target_chembl_id={quote(target['chemblId'], safe='')}"
            f"&standard_type__in={quote(activity_type, safe=',')}"
            "&standard_value__isnull=false"
            "&molecule_chembl_id__isnull=false"
            f"&order_by=standard_value&limit={size * 8}"
        )
        activities = activity_payload.get("activities") if isinstance(activity_payload.get("activities"), list) else []
        # Keep the most potent (lowest) measured value per compound. The source is pre-sorted
        # ascending by standard_value, so the first occurrence of each molecule is its best.
        best_by_molecule: Dict[str, Dict[str, Any]] = {}
        for item in activities:
            if not isinstance(item, dict):
                continue
            molecule_id = str(item.get("molecule_chembl_id") or "").strip()
            try:
                value = float(item.get("standard_value"))
            except (TypeError, ValueError):
                continue
            if not molecule_id:
                continue
            units = str(item.get("standard_units") or "nM").strip()
            rank = self._potency_rank_nm(value, units)
            existing = best_by_molecule.get(molecule_id)
            if existing is None or rank < existing["_rank"]:
                best_by_molecule[molecule_id] = {
                    "molecule_chembl_id": molecule_id,
                    "type": str(item.get("standard_type") or "").strip(),
                    "value": value,
                    "units": units,
                    "name_hint": str(item.get("molecule_pref_name") or "").strip(),
                    "_rank": rank,
                }
        # Rank on the nM-normalized potency — raw ordering mixed units (0.2 ug.mL-1 outranked
        # sub-nM rows, live-verified). Non-molar rows keep their value, ranked last.
        ranked = sorted(best_by_molecule.values(), key=lambda row: row["_rank"])[:size]
        for row in ranked:
            row.pop("_rank", None)
        if not ranked:
            return {"source": "chembl", "query": query, "target": target, "count": 0, "results": []}

        hydrated = self._hydrate_chembl_molecules([row["molecule_chembl_id"] for row in ranked])
        results: List[Dict[str, Any]] = []
        for row in ranked:
            info = hydrated.get(row["molecule_chembl_id"], {})
            # Flat record so the observation summarizer surfaces every field (smiles, value,
            # activityType, units) at the top level — the same shape pubchem/rcsb use, not a
            # nested compound/activity object the summarizer cannot reach.
            results.append(
                {
                    "title": info.get("name") or row["name_hint"] or row["molecule_chembl_id"],
                    "chemblId": row["molecule_chembl_id"],
                    "smiles": info.get("smiles", ""),
                    "activityType": row["type"],
                    "value": row["value"],
                    "units": row["units"],
                }
            )
        return {
            "source": "chembl",
            "query": query,
            "target": target,
            "count": len(results),
            "results": results,
        }

    def _search_pubmed(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        query = _required_string(arguments, "query", label="PubMed search")
        size = _resolve_size(arguments, 5)
        # NCBI asks every E-utility request to carry tool/email (+ api_key when one exists)
        # so they can contact the operator instead of blocking the IP (NBK25497).
        ident = self._ncbi_ident_params()
        esearch = self._get_json(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
            f"db=pubmed&term={quote(query)}&retmode=json&retmax={size}&{ident}"
        )
        esearch_result = esearch.get("esearchresult") if isinstance(esearch.get("esearchresult"), dict) else {}
        ids = [str(item) for item in (esearch_result.get("idlist") or []) if str(item).isdigit()]
        if not ids:
            return {"source": "pubmed", "query": query, "count": 0, "results": []}
        esummary = self._get_json(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?"
            f"db=pubmed&id={quote(','.join(ids), safe=',')}&retmode=json&{ident}"
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
