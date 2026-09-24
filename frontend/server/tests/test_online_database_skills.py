"""Online database skills: search/resolve contracts and planner-loop regressions.

Covers the generalization fix:
- uniprot.search maps a gene/protein name to a canonical accession (DHODH -> Q02127).
- pubchem.search resolves a name/code by name (PF06882961 -> CID 134611040), never a
  fabricated numeric CID, and reports no-match honestly instead of substituting.
- pubchem.resolve is CID-only.
"""

from __future__ import annotations

import email.utils
import json
import sys
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from management_api.copilot import CopilotAssistant
from management_api.copilot_skill_harness import CopilotSkillDefinition, CopilotSkillHarness
from management_api.copilot_skills.online_databases import OnlineDatabaseSkills, OnlineSkillDefinition
from management_api.copilot_skills.compute_skills import register_compute_skills
from tests.helpers import FakeResponse, NullLogger, is_phase2_request


# --------------------------------------------------------------------------- #
# HTTP-level unit tests: real handlers, stubbed transport (no network).
# --------------------------------------------------------------------------- #


class FakeHttpResponse:
    def __init__(self, *, ok: bool = True, status_code: int = 200, payload: Dict[str, Any] | None = None, text: str = "", json_raises: Exception | None = None, headers: Dict[str, str] | None = None) -> None:
        self.ok = ok
        self.status_code = status_code
        self.text = text
        self._payload = payload or {}
        self._json_raises = json_raises
        self.headers = headers or {}

    def json(self) -> Dict[str, Any]:
        if self._json_raises is not None:
            raise self._json_raises
        return self._payload


class FakeHttpSession:
    """Mimics requests.Session.get/post for OnlineDatabaseSkills.

    Each queued entry may be a FakeHttpResponse OR an Exception instance; an Exception is raised
    when its turn arrives, so transient-failure retry paths can be exercised without a network.
    """

    def __init__(self, responses: List[Any]) -> None:
        self.responses = list(responses)
        self.requests: List[Dict[str, Any]] = []

    def _next(self, url: str, method: str) -> Any:
        self.requests.append({"url": url, "method": method, "t": time.monotonic()})
        if not self.responses:
            raise AssertionError(f"unexpected HTTP {method}")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url: str, headers: Dict[str, str] | None = None, timeout: float | None = None, **kwargs: Any) -> FakeHttpResponse:
        return self._next(url, "GET")

    def post(self, url: str, headers: Dict[str, str] | None = None, data: Any = None, timeout: float | None = None, **kwargs: Any) -> FakeHttpResponse:
        return self._next(url, "POST")


def _skills(responses: List[FakeHttpResponse]) -> OnlineDatabaseSkills:
    return OnlineDatabaseSkills(session=FakeHttpSession(responses), timeout_seconds=3)


class UniprotSearchTests(unittest.TestCase):
    def test_returns_ranked_candidates_with_sequence(self):
        payload = {
            "hitCount": 1,
            "results": [
                {
                    "primaryAccession": "Q02127",
                    "uniProtkbId": "PYRD_HUMAN",
                    "entryType": "UniProtKB reviewed (Swiss-Prot)",
                    "organism": {"scientificName": "Homo sapiens"},
                    "genes": [{"geneName": {"value": "DHODH"}}],
                    "proteinDescription": {
                        "recommendedName": {"fullName": {"value": "Dihydroorotate dehydrogenase (quinone), mitochondrial"}}
                    },
                    "sequence": {"value": "MTPRKRGTG ACDE\nFGHIK", "length": 15},
                }
            ],
        }
        result = _skills([FakeHttpResponse(payload=payload)]).execute(
            "uniprot.search", {"query": "gene:DHODH AND organism_id:9606"}
        )
        self.assertEqual(result["count"], 1)
        top = result["results"][0]
        self.assertEqual(top["accession"], "Q02127")
        self.assertEqual(top["geneNames"], "DHODH")
        self.assertEqual(top["organism"], "Homo sapiens")
        self.assertTrue(top["reviewed"])
        # the sequence is returned cleaned (whitespace stripped, uppercased) so a single
        # search answers sequence questions without a separate resolve step
        self.assertEqual(top["sequence"], "MTPRKRGTGACDEFGHIK")

    def test_zero_hits_returns_empty_and_does_not_raise(self):
        result = _skills([FakeHttpResponse(payload={"hitCount": 0, "results": []})]).execute(
            "uniprot.search", {"query": "NONEXISTENT_GENE_XYZ"}
        )
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["results"], [])

    def test_reviewed_canonical_entry_ranked_above_unreviewed_fragment(self):
        payload = {
            "results": [
                {
                    "primaryAccession": "A0ACI8SEY4",
                    "uniProtkbId": "A0ACI8SEY4_HUMAN",
                    "entryType": "UniProtKB unreviewed (TrEMBL)",
                    "organism": {"scientificName": "Homo sapiens"},
                    "genes": [{"geneName": {"value": "DHODH"}}],
                    "sequence": {"length": 392},
                },
                {
                    "primaryAccession": "Q02127",
                    "uniProtkbId": "PYRD_HUMAN",
                    "entryType": "UniProtKB reviewed (Swiss-Prot)",
                    "organism": {"scientificName": "Homo sapiens"},
                    "genes": [{"geneName": {"value": "DHODH"}}],
                    "proteinDescription": {"recommendedName": {"fullName": {"value": "Dihydroorotate dehydrogenase"}}},
                    "sequence": {"length": 395},
                },
            ]
        }
        result = _skills([FakeHttpResponse(payload=payload)]).execute(
            "uniprot.search", {"query": "gene:DHODH AND organism_id:9606"}
        )
        self.assertEqual(result["count"], 2)
        # the reviewed canonical entry surfaces above the unreviewed fragment even though
        # the source listed the fragment first
        self.assertEqual(result["results"][0]["accession"], "Q02127")
        self.assertTrue(result["results"][0]["reviewed"])
        self.assertEqual(result["results"][1]["accession"], "A0ACI8SEY4")

    def test_empty_query_raises(self):
        with self.assertRaises(ValueError):
            _skills([]).execute("uniprot.search", {"query": "   "})


class PubchemSearchTests(unittest.TestCase):
    def test_name_match_returns_smiles_and_echoes_query(self):
        payload = {
            "PropertyTable": {
                "Properties": [
                    {"CID": 134611040, "Title": "benzimidazole-6-carboxylic acid", "IsomericSMILES": "C1CO[C@@H]1CN2"}
                ]
            }
        }
        result = _skills([FakeHttpResponse(payload=payload)]).execute("pubchem.search", {"identifier": "PF06882961"})
        self.assertEqual(result["count"], 1)
        top = result["results"][0]
        self.assertEqual(top["cid"], "134611040")
        self.assertEqual(top["smiles"], "C1CO[C@@H]1CN2")
        self.assertEqual(result["query"], "PF06882961")

    def test_http_404_returns_empty_and_does_not_raise(self):
        result = _skills([FakeHttpResponse(ok=False, status_code=404, text="not found")]).execute(
            "pubchem.search", {"identifier": "NOPE9999"}
        )
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["results"], [])


class RcsbSearchTests(unittest.TestCase):
    def test_returns_ranked_entries_enriched_from_data_api(self):
        search_payload = {"total_count": 2, "result_set": [{"identifier": "2Z4Q", "score": 0.9}, {"identifier": "8JFQ", "score": 0.8}]}
        entry_payload = {"struct": {"title": "Crystal structure of EGFR"}, "rcsb_entry_info": {"resolution_combined": [2.3]}}
        session = FakeHttpSession(
            [
                FakeHttpResponse(payload=search_payload),  # POST search
                FakeHttpResponse(payload=entry_payload),  # GET entry 2Z4Q
                FakeHttpResponse(payload=entry_payload),  # GET entry 8JFQ
            ]
        )
        result = OnlineDatabaseSkills(session=session, timeout_seconds=3).execute("rcsb.search", {"text": "EGFR human", "size": 2})
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["results"][0]["pdbId"], "2Z4Q")
        self.assertEqual(result["results"][0]["title"], "Crystal structure of EGFR")
        self.assertEqual(result["results"][0]["resolution"], 2.3)
        # the search call hit the RCSB search endpoint
        self.assertTrue(any("search.rcsb.org" in r["url"] for r in session.requests))

    def test_records_expose_only_guaranteed_download_links(self):
        # ROOT-CAUSE CONTRACT: an RCSB record field is a promise that the link works. mmCIF is
        # the master archive format (every entry has it); the legacy PDB-format file exists
        # only for some entries (their .pdb URL answers 404). Records must therefore carry the
        # cifUrl and NEVER a pdbUrl — exposing a possibly-dead link is what caused dead
        # downloads downstream (and would invite fallback band-aids instead of this fix).
        search_payload = {"total_count": 1, "result_set": [{"identifier": "9VO8", "score": 1.0}]}
        entry_payload = {"struct": {"title": "HPK1 complex"}}
        session = FakeHttpSession(
            [FakeHttpResponse(payload=search_payload), FakeHttpResponse(payload=entry_payload)]
        )
        result = OnlineDatabaseSkills(session=session, timeout_seconds=3).execute("rcsb.search", {"text": "HPK1", "size": 1})
        record = result["results"][0]
        self.assertNotIn("pdbUrl", record, "RCSB records must not expose the possibly-missing PDB-format link")
        self.assertEqual(record["cifUrl"], "https://files.rcsb.org/download/9VO8.cif")

    def test_empty_query_raises(self):
        with self.assertRaises(ValueError):
            _skills([]).execute("rcsb.search", {"text": "   "})


class AlphafoldResolveTests(unittest.TestCase):
    def test_returns_predicted_structure_with_confidence(self):
        # AlphaFold DB answers with a list of entries.
        entry_payload = [{
            "uniprotAccession": "Q02127",
            "uniprotDescription": "Dihydroorotate dehydrogenase",
            "organismScientificName": "Homo sapiens",
            "gene": "DHODH",
            "globalMetricValue": 92.5,
            "fractionPlddtConfident": 0.8,
            "fractionPlddtVeryHigh": 0.6,
            "pdbUrl": "https://alphafold.ebi.ac.uk/files/AF-Q02127-F1-model_v6.pdb",
            "cifUrl": "https://alphafold.ebi.ac.uk/files/AF-Q02127-F1-model_v6.cif",
        }]
        result = _skills([FakeHttpResponse(payload=entry_payload)]).execute("alphafold.resolve", {"identifier": "q02127"})
        self.assertEqual(result["accession"], "Q02127")
        self.assertEqual(result["organism"], "Homo sapiens")
        self.assertEqual(result["avgPlddt"], 92.5)
        self.assertIn("AF-Q02127", result["pdbUrl"])

    def test_no_structure_raises(self):
        with self.assertRaises(RuntimeError):
            _skills([FakeHttpResponse(payload=[{"uniprotAccession": "X"}])]).execute("alphafold.resolve", {"identifier": "X"})


class ResolveNotFoundClassificationTests(unittest.TestCase):
    """A source's authoritative 404 (no such record) must classify NO_MATCH, not FAILED.

    An HTTP 404 is the source answering "this entity does not exist" — the planner must tell the
    user nothing was found, never "source unavailable" (which is reserved for transport failures).
    """

    def test_uniprot_resolve_404_is_no_match(self):
        result = _skills([FakeHttpResponse(ok=False, status_code=404, text="not found")]).execute(
            "uniprot.resolve", {"identifier": "Q9ZZZ9"})
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["results"], [])
        observation = {"ok": True, "values": [result]}
        self.assertEqual(CopilotSkillHarness.classify_observation(observation), "NO_MATCH")

    def test_rcsb_resolve_404_is_no_match(self):
        result = _skills([FakeHttpResponse(ok=False, status_code=404, text="not found")]).execute(
            "rcsb.resolve", {"identifier": "ZZZZZZ"})
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["results"], [])
        observation = {"ok": True, "values": [result]}
        self.assertEqual(CopilotSkillHarness.classify_observation(observation), "NO_MATCH")

    def test_alphafold_resolve_404_is_no_match(self):
        result = _skills([FakeHttpResponse(ok=False, status_code=404, text="not found")]).execute(
            "alphafold.resolve", {"identifier": "P99999"})
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["results"], [])
        observation = {"ok": True, "values": [result]}
        self.assertEqual(CopilotSkillHarness.classify_observation(observation), "NO_MATCH")

    def test_resolve_5xx_stays_failed_not_no_match(self):
        # A transient server error is NOT "nothing found": it must keep raising so the harness
        # classifies it FAILED (source unavailable), never conflated with an authoritative 404.
        session = FakeHttpSession(
            [FakeHttpResponse(ok=False, status_code=503, text="service unavailable")]
            * OnlineDatabaseSkills._RETRY_MAX_ATTEMPTS
        )
        skills = OnlineDatabaseSkills(session=session, timeout_seconds=3)
        with patch("management_api.copilot_skills.online_databases.time.sleep"):
            with self.assertRaises(RuntimeError):
                skills.execute("uniprot.resolve", {"identifier": "P00533"})


class ChemblBioactivityTests(unittest.TestCase):
    def setUp(self) -> None:
        # these tests fire multiple stubbed ChEMBL calls in succession; neutralize the real
        # rate-limit sleep so the suite stays fast. Rate-limit behavior is covered separately.
        patcher = patch("management_api.copilot_skills.online_databases.time.sleep")
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_returns_compound_with_ranked_deduped_targets(self):
        mol_payload = {"molecules": [{"molecule_chembl_id": "CHEMBL941", "pref_name": "IMATINIB"}]}
        activity_payload = {"activities": [
            {"target_pref_name": "Receptor tyrosine-protein kinase erbB-2", "standard_type": "IC50", "standard_value": 0.06, "standard_units": "nM", "organism": "Homo sapiens"},
            {"target_pref_name": "Epidermal growth factor receptor", "standard_type": "IC50", "standard_value": 0.11, "standard_units": "nM", "organism": "Homo sapiens"},
            {"target_pref_name": "Epidermal growth factor receptor", "standard_type": "IC50", "standard_value": 5.0, "standard_units": "nM", "organism": "Homo sapiens"},  # dup, less potent -> dropped
            {"target_pref_name": "Unchecked", "standard_type": "IC50", "standard_value": 1.0, "standard_units": "nM"},  # filtered out
        ]}
        session = FakeHttpSession([FakeHttpResponse(payload=mol_payload), FakeHttpResponse(payload=activity_payload)])
        result = OnlineDatabaseSkills(session=session, timeout_seconds=3).execute("chembl.bioactivity", {"query": "imatinib", "size": 5})
        self.assertEqual(result["compound"]["chemblId"], "CHEMBL941")
        self.assertEqual(result["count"], 2)  # erbB-2 + EGFR (dup dropped, Unchecked filtered)
        self.assertEqual(result["results"][0]["target"], "Receptor tyrosine-protein kinase erbB-2")
        self.assertEqual(result["results"][0]["value"], 0.06)

    def test_unknown_compound_returns_empty(self):
        result = _skills([FakeHttpResponse(payload={"molecules": []})]).execute("chembl.bioactivity", {"query": "NOTAREALDRUG"})
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["results"], [])


class ChemblTargetActivityTests(unittest.TestCase):
    """Target-directed bioactivity: target -> ranked active compounds with potency + SMILES.

    The inverse of chembl.bioactivity (compound -> targets). Exercises the canonical ChEMBL
    target->activity->molecule join on the stable filter API.
    """

    def setUp(self) -> None:
        # neutralize the real ChEMBL rate-limit sleep for suite speed; rate limiting is covered
        # in ResilienceTests with real timing.
        patcher = patch("management_api.copilot_skills.online_databases.time.sleep")
        self.addCleanup(patcher.stop)
        patcher.start()

    _TARGET_PAYLOAD = {
        "targets": [
            {
                "target_chembl_id": "CHEMBL4618",
                "pref_name": "MAP kinase kinase kinase kinase 1",
                "organism": "Homo sapiens",
                "target_type": "SINGLE PROTEIN",
            }
        ]
    }
    # ordered by standard_value ascending; CHEMBL1 appears twice and the weaker duplicate drops
    _ACTIVITY_PAYLOAD = {
        "activities": [
            {"molecule_chembl_id": "CHEMBL2", "standard_type": "IC50", "standard_value": 2.0, "standard_units": "nM", "molecule_pref_name": "drug B"},
            {"molecule_chembl_id": "CHEMBL1", "standard_type": "IC50", "standard_value": 10.0, "standard_units": "nM", "molecule_pref_name": "drug A"},
            {"molecule_chembl_id": "CHEMBL1", "standard_type": "IC50", "standard_value": 50.0, "standard_units": "nM"},  # dup, less potent -> dropped
        ]
    }
    _MOLECULE_PAYLOAD = {
        "molecules": [
            {"molecule_chembl_id": "CHEMBL1", "pref_name": "drug A", "molecule_structures": {"canonical_smiles": "CCO"}},
            {"molecule_chembl_id": "CHEMBL2", "pref_name": "drug B", "molecule_structures": {"canonical_smiles": "CC"}},
        ]
    }

    def test_accession_path_returns_ranked_compounds_with_smiles(self):
        session = FakeHttpSession([
            FakeHttpResponse(payload=self._TARGET_PAYLOAD),    # target lookup by accession
            FakeHttpResponse(payload=self._ACTIVITY_PAYLOAD),  # activities
            FakeHttpResponse(payload=self._MOLECULE_PAYLOAD),  # molecule hydration
        ])
        result = OnlineDatabaseSkills(session=session, timeout_seconds=3).execute(
            "chembl.target_activity", {"query": "HPK1", "accession": "Q92918", "size": 5}
        )
        # the accession path resolves the target via target_components__accession
        self.assertIn("target_components__accession=Q92918", session.requests[0]["url"])
        self.assertEqual(result["target"]["chemblId"], "CHEMBL4618")
        self.assertEqual(result["target"]["organism"], "Homo sapiens")
        self.assertEqual(result["count"], 2)
        # ranked by potency: drug B (2 nM) above drug A (10 nM). Records are FLAT so the
        # observation summarizer can surface smiles/value/activityType/units to the model.
        top = result["results"][0]
        self.assertEqual(top["chemblId"], "CHEMBL2")
        self.assertEqual(top["title"], "drug B")
        self.assertEqual(top["smiles"], "CC")
        self.assertEqual(top["activityType"], "IC50")
        self.assertEqual(top["value"], 2.0)
        self.assertEqual(top["units"], "nM")
        self.assertEqual(result["results"][1]["chemblId"], "CHEMBL1")
        self.assertEqual(result["results"][1]["smiles"], "CCO")
        # exactly three network calls (target, activity, molecule) — no retry, no extra hydration
        self.assertEqual(len(session.requests), 3)

    def test_free_text_name_uses_pref_name_icontains(self):
        session = FakeHttpSession([
            FakeHttpResponse(payload=self._TARGET_PAYLOAD),
            FakeHttpResponse(payload=self._ACTIVITY_PAYLOAD),
            FakeHttpResponse(payload=self._MOLECULE_PAYLOAD),
        ])
        OnlineDatabaseSkills(session=session, timeout_seconds=3).execute(
            "chembl.target_activity", {"query": "epidermal growth factor receptor"}
        )
        # no accession -> falls back to preferred-name matching
        self.assertIn("pref_name__icontains=", session.requests[0]["url"])

    def test_target_chembl_id_direct_lookup(self):
        session = FakeHttpSession([
            FakeHttpResponse(payload=self._TARGET_PAYLOAD["targets"][0]),  # single record, not list-wrapped
            FakeHttpResponse(payload=self._ACTIVITY_PAYLOAD),
            FakeHttpResponse(payload=self._MOLECULE_PAYLOAD),
        ])
        result = OnlineDatabaseSkills(session=session, timeout_seconds=3).execute(
            "chembl.target_activity", {"query": "EGFR", "target_chembl_id": "CHEMBL4618"}
        )
        self.assertIn("/target/CHEMBL4618.json", session.requests[0]["url"])
        self.assertEqual(result["target"]["chemblId"], "CHEMBL4618")

    def test_no_target_match_returns_empty_and_does_not_raise(self):
        result = _skills([FakeHttpResponse(payload={"targets": []})]).execute(
            "chembl.target_activity", {"query": "NOTAREALTARGET_XYZ"}
        )
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["results"], [])
        # only the target lookup happened — no activity/molecule calls when no target resolved
        # (the FakeHttpSession would raise AssertionError on a second unexpected request)

    def test_target_found_but_no_activity_returns_empty_with_target_echoed(self):
        session = FakeHttpSession([
            FakeHttpResponse(payload=self._TARGET_PAYLOAD),
            FakeHttpResponse(payload={"activities": []}),
        ])
        result = OnlineDatabaseSkills(session=session, timeout_seconds=3).execute(
            "chembl.target_activity", {"query": "HPK1", "accession": "Q92918"}
        )
        # the resolved target is echoed even when no active compounds are known
        self.assertEqual(result["target"]["chemblId"], "CHEMBL4618")
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["results"], [])

    def test_organism_preference_picks_human_over_ortholog(self):
        target_payload = {
            "targets": [
                {"target_chembl_id": "CHEMBL_MOUSE", "pref_name": "MAP4K1", "organism": "Mus musculus", "target_type": "SINGLE PROTEIN"},
                {"target_chembl_id": "CHEMBL_HUMAN", "pref_name": "MAP4K1", "organism": "Homo sapiens", "target_type": "SINGLE PROTEIN"},
            ]
        }
        session = FakeHttpSession([
            FakeHttpResponse(payload=target_payload),
            FakeHttpResponse(payload={"activities": []}),
        ])
        result = OnlineDatabaseSkills(session=session, timeout_seconds=3).execute(
            "chembl.target_activity", {"query": "MAP4K1", "organism": "Homo sapiens"}
        )
        # the human ortholog is selected over mouse by the stated organism preference
        self.assertEqual(result["target"]["chemblId"], "CHEMBL_HUMAN")
        self.assertEqual(result["target"]["organism"], "Homo sapiens")
        self.assertEqual(result["target"]["matchedTargets"], 2)

    def test_unstated_organism_is_not_silently_defaulted(self):
        # ENTITY IDENTITY contract: an unstated organism is an unresolved choice, never a
        # silent human default. Without 'organism', the resolver applies NO preference and
        # returns its best match WITH the organism stated and the candidate count visible —
        # the planner (not the source) resolves the ambiguity against the user's intent.
        target_payload = {
            "targets": [
                {"target_chembl_id": "CHEMBL_MOUSE", "pref_name": "MAP4K1", "organism": "Mus musculus", "target_type": "SINGLE PROTEIN"},
                {"target_chembl_id": "CHEMBL_HUMAN", "pref_name": "MAP4K1", "organism": "Homo sapiens", "target_type": "SINGLE PROTEIN"},
            ]
        }
        session = FakeHttpSession([
            FakeHttpResponse(payload=target_payload),
            FakeHttpResponse(payload={"activities": []}),
        ])
        result = OnlineDatabaseSkills(session=session, timeout_seconds=3).execute(
            "chembl.target_activity", {"query": "MAP4K1"}
        )
        self.assertEqual(result["target"]["matchedTargets"], 2)
        self.assertIn(result["target"]["organism"], ("Mus musculus", "Homo sapiens"))
        # No silent human substitution: whichever record won, its organism is stated.

    def test_target_activity_value_column_feeds_compute_aggregate(self):
        # Cross-skill composition: the flat top-level `value` (potency) the ChEMBL skill emits is
        # exactly what compute.aggregate consumes via $fromObservation all:true — so the planner
        # can answer "find inhibitors AND give the mean IC50" in one find+compute chain. Guards
        # against re-nesting the value under an activity object (which would break the column).
        session = FakeHttpSession([
            FakeHttpResponse(payload=self._TARGET_PAYLOAD),
            FakeHttpResponse(payload=self._ACTIVITY_PAYLOAD),
            FakeHttpResponse(payload=self._MOLECULE_PAYLOAD),
        ])
        skills = OnlineDatabaseSkills(session=session, timeout_seconds=3)
        register_compute_skills(skills)
        chembl = skills.execute("chembl.target_activity", {"query": "HPK1", "accession": "Q92918", "size": 5})
        # mirror what {"$fromObservation": "inh", "field": "value", "all": true} materializes
        values = [row["value"] for row in chembl["results"]]
        agg = skills.execute("compute.aggregate", {"values": values})
        summary = agg["results"][0]
        # _ACTIVITY_PAYLOAD dedups to IC50 2.0 (CHEMBL2) and 10.0 (CHEMBL1)
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["min"], 2.0)
        self.assertEqual(summary["max"], 10.0)
        self.assertEqual(summary["mean"], 6.0)


class PubmedSearchTests(unittest.TestCase):
    def test_returns_articles_from_esummary(self):
        esearch_payload = {"esearchresult": {"count": "2", "idlist": ["111", "222"]}}
        esummary_payload = {"result": {
            "uids": ["111", "222"],
            "111": {"title": "Paper One", "source": "Nature", "pubdate": "2024 Jan", "authors": [{"name": "A B"}, {"name": "C D"}]},
            "222": {"title": "Paper Two", "source": "Cell", "pubdate": "2023", "authors": [{"name": "E F"}]},
        }}
        session = FakeHttpSession([FakeHttpResponse(payload=esearch_payload), FakeHttpResponse(payload=esummary_payload)])
        result = OnlineDatabaseSkills(session=session, timeout_seconds=3).execute("pubmed.search", {"query": "EGFR", "size": 2})
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["results"][0]["pmid"], "111")
        self.assertEqual(result["results"][0]["title"], "Paper One")
        self.assertEqual(result["results"][0]["authors"], "A B et al.")
        self.assertEqual(result["results"][1]["authors"], "E F")
        self.assertIn("pubmed.ncbi.nlm.nih.gov", result["results"][0]["url"])

    def test_no_results_returns_empty(self):
        result = _skills([FakeHttpResponse(payload={"esearchresult": {"idlist": []}})]).execute("pubmed.search", {"query": "NONEXISTENTXYZ"})
        self.assertEqual(result["count"], 0)

    def test_missing_pubdate_does_not_crash(self):
        # PubMed entries occasionally omit pubdate (books, preprints); year becomes "" instead of
        # raising IndexError and failing the whole search.
        esearch_payload = {"esearchresult": {"idlist": ["111"]}}
        esummary_payload = {"result": {"uids": ["111"], "111": {"title": "No Date Paper", "source": "Nature"}}}
        session = FakeHttpSession([FakeHttpResponse(payload=esearch_payload), FakeHttpResponse(payload=esummary_payload)])
        result = OnlineDatabaseSkills(session=session, timeout_seconds=3).execute("pubmed.search", {"query": "x", "size": 1})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["year"], "")


class ClinicalTrialsSearchTests(unittest.TestCase):
    def test_returns_trials_from_api(self):
        payload = {"studies": [
            {"protocolSection": {
                "identificationModule": {"nctId": "NCT12345", "briefTitle": "Test Trial"},
                "statusModule": {"overallStatus": "RECRUITING"},
                "designModule": {"phases": ["PHASE2"]},
                "conditionsModule": {"conditions": ["Cancer", "EGFR Mutation"]},
            }},
        ]}
        result = _skills([FakeHttpResponse(payload=payload)]).execute("clinicaltrials.search", {"query": "EGFR inhibitor", "size": 1})
        self.assertEqual(result["count"], 1)
        top = result["results"][0]
        self.assertEqual(top["nctId"], "NCT12345")
        self.assertEqual(top["status"], "RECRUITING")
        self.assertEqual(top["phase"], "PHASE2")
        self.assertIn("EGFR Mutation", top["conditions"])


class PubchemSearchRoutingTests(unittest.TestCase):
    """pubchem.search exposes PubChem's real PUG-REST namespaces."""

    def _url_for(self, arguments):
        session = FakeHttpSession([FakeHttpResponse(payload={"PropertyTable": {"Properties": [{"CID": 1, "Title": "x", "IsomericSMILES": "C"}]}})])
        OnlineDatabaseSkills(session=session, timeout_seconds=3).execute("pubchem.search", arguments)
        return session.requests[0]["url"]

    def test_default_namespace_is_name(self):
        self.assertIn("/compound/name/aspirin/", self._url_for({"identifier": "aspirin"}))

    def test_cid_namespace_hits_cid_endpoint(self):
        self.assertIn("/compound/cid/2244/", self._url_for({"identifier": "2244", "namespace": "cid"}))

    def test_inchikey_namespace_hits_inchikey_endpoint(self):
        self.assertIn("/compound/inchikey/RYYVLZVUVIJVGH-UHFFFAOYSA-N/", self._url_for({"identifier": "RYYVLZVUVIJVGH-UHFFFAOYSA-N", "namespace": "inchikey"}))

    def test_smiles_namespace_is_url_encoded(self):
        url = self._url_for({"identifier": "c1ccccc1", "namespace": "smiles"})
        self.assertIn("/compound/smiles/", url)


class SkillResultCacheTests(unittest.TestCase):
    """The read-skill cache must hand back independent copies so a caller cannot mutate the cached
    value (or another caller's result). Pins the deepcopy on both cache store and cache load."""

    def _skills_with_one_uniprot_hit(self) -> tuple[OnlineDatabaseSkills, FakeHttpSession]:
        payload = {
            "results": [{
                "primaryAccession": "Q02127",
                "uniProtkbId": "PYRD_HUMAN",
                "entryType": "UniProtKB reviewed (Swiss-Prot)",
                "organism": {"scientificName": "Homo sapiens"},
                "genes": [{"geneName": {"value": "DHODH"}}],
                "sequence": {"value": "MTPRKRGTG", "length": 8},
            }]
        }
        session = FakeHttpSession([FakeHttpResponse(payload=payload)])
        return OnlineDatabaseSkills(session=session, timeout_seconds=3), session

    def test_second_call_is_served_from_cache_without_a_network_hit(self):
        skills, session = self._skills_with_one_uniprot_hit()
        args = {"query": "gene:DHODH AND organism_id:9606"}
        first = skills.execute("uniprot.search", args)
        second = skills.execute("uniprot.search", args)
        self.assertEqual(first, second)
        # only the first call hit the network; the second was served from the in-memory cache
        self.assertEqual(len(session.requests), 1)

    def test_returned_cache_copies_are_independent_of_caller_mutation(self):
        skills, _session = self._skills_with_one_uniprot_hit()
        args = {"query": "gene:DHODH AND organism_id:9606"}
        first = skills.execute("uniprot.search", args)
        # corrupt the first result the way a careless caller might
        first["results"].append({"accession": "POLLUTED"})
        first["results"][0]["accession"] = "MUTATED"

        second = skills.execute("uniprot.search", args)
        # the cached copy is unaffected by mutation of the first returned object (store-side deepcopy)
        self.assertEqual(len(second["results"]), 1)
        self.assertEqual(second["results"][0]["accession"], "Q02127")

        # mutating the second (cache-hit) result must not poison a third call either (load-side deepcopy)
        second["results"][0]["accession"] = "POISONED"
        third = skills.execute("uniprot.search", args)
        self.assertEqual(third["results"][0]["accession"], "Q02127")
        self.assertIsNot(first, second)
        self.assertIsNot(second, third)


# --------------------------------------------------------------------------- #
# Resilience: transient upstream failures (the documented EBI/ChEMBL HTTP 500
# condition) must be retried with backoff; deterministic 4xx must not be.
# --------------------------------------------------------------------------- #


class ResilienceTests(unittest.TestCase):
    """Bounded exponential backoff on transient errors; 4xx stays honest."""

    @patch("management_api.copilot_skills.online_databases.time.sleep")
    def test_transient_500_then_200_succeeds_after_retry(self, _sleep):
        # ChEMBL molecule/search flaps 500 once, then succeeds — the canonical EBI hiccup.
        session = FakeHttpSession([
            FakeHttpResponse(ok=False, status_code=500, text="server error"),
            FakeHttpResponse(payload={"molecules": []}),
        ])
        result = OnlineDatabaseSkills(session=session, timeout_seconds=3).execute(
            "chembl.bioactivity", {"query": "imatinib"}
        )
        # the retry recovered the call and reported an honest empty result (no crash)
        self.assertEqual(result["count"], 0)
        self.assertEqual(len(session.requests), 2)  # exactly one retry, then success

    @patch("management_api.copilot_skills.online_databases.time.sleep")
    def test_persistent_500_raises_after_exhausting_retries(self, _sleep):
        session = FakeHttpSession([
            FakeHttpResponse(ok=False, status_code=500, text="down")
            for _ in range(OnlineDatabaseSkills._RETRY_MAX_ATTEMPTS)
        ])
        with self.assertRaises(RuntimeError):
            OnlineDatabaseSkills(session=session, timeout_seconds=3).execute(
                "chembl.bioactivity", {"query": "imatinib"}
            )
        # retried up to the configured max attempts, then surfaced the failure honestly
        self.assertEqual(len(session.requests), OnlineDatabaseSkills._RETRY_MAX_ATTEMPTS)

    @patch("management_api.copilot_skills.online_databases.time.sleep")
    def test_connection_error_retries_then_raises_unreachable(self, _sleep):
        session = FakeHttpSession([
            requests.exceptions.Timeout("timed out")
            for _ in range(OnlineDatabaseSkills._RETRY_MAX_ATTEMPTS)
        ])
        with self.assertRaises(RuntimeError) as ctx:
            OnlineDatabaseSkills(session=session, timeout_seconds=3).execute(
                "chembl.bioactivity", {"query": "imatinib"}
            )
        self.assertIn("unreachable", str(ctx.exception))
        self.assertEqual(len(session.requests), OnlineDatabaseSkills._RETRY_MAX_ATTEMPTS)

    def test_404_is_not_retried(self):
        # 404 is deterministic (no authoritative match); it must short-circuit to honest-empty
        # in a single request, never burning retries.
        session = FakeHttpSession([FakeHttpResponse(ok=False, status_code=404, text="not found")])
        result = OnlineDatabaseSkills(session=session, timeout_seconds=3).execute(
            "pubchem.search", {"identifier": "NOPE9999"}
        )
        self.assertEqual(result["count"], 0)
        self.assertEqual(len(session.requests), 1)

    def test_non_json_200_reported_cleanly_and_not_retried(self):
        # HTTP 200 with a non-JSON body (empty response or an HTML error page) must surface as a
        # clean source failure (SOURCE UNAVAILABLE downstream), not a raw JSONDecodeError. It is
        # not a transient 5xx, so it is not retried.
        session = FakeHttpSession([
            FakeHttpResponse(ok=True, status_code=200, text="<!doctype html>error", json_raises=ValueError("Expecting value")),
        ])
        with self.assertRaises(RuntimeError) as ctx:
            OnlineDatabaseSkills(session=session, timeout_seconds=3).execute(
                "chembl.bioactivity", {"query": "imatinib"}
            )
        self.assertIn("non-JSON", str(ctx.exception))
        self.assertEqual(len(session.requests), 1)

    def test_chembl_requests_are_spaced_to_the_documented_rate_limit(self):
        # ChEMBL's documented ceiling is <=1 req/s without an API key; the official client throttles
        # automatically, and so must we — two sequential calls to www.ebi.ac.uk are spaced ~1s apart.
        session = FakeHttpSession([
            FakeHttpResponse(payload={"molecules": [{"molecule_chembl_id": "CHEMBL1", "pref_name": "x"}]}),
            FakeHttpResponse(payload={"activities": []}),
        ])
        OnlineDatabaseSkills(session=session, timeout_seconds=3).execute(
            "chembl.bioactivity", {"query": "imatinib"}
        )
        times = [req["t"] for req in session.requests]
        self.assertGreaterEqual(len(times), 2)
        self.assertGreaterEqual(times[1] - times[0], 0.9)

    def test_ncbi_requests_are_spaced_to_the_documented_rate_limit(self):
        # NCBI's documented ceiling is 3 req/s without an API key (NBK25497): the esearch +
        # esummary pair of one pubmed.search is spaced ~0.34s apart, and the requests carry the
        # tool identification NCBI asks for.
        session = FakeHttpSession([
            FakeHttpResponse(payload={"esearchresult": {"idlist": ["111"]}}),
            FakeHttpResponse(payload={"result": {"uids": ["111"], "111": {"title": "T"}}}),
        ])
        OnlineDatabaseSkills(session=session, timeout_seconds=3).execute(
            "pubmed.search", {"query": "x", "size": 1}
        )
        times = [req["t"] for req in session.requests]
        self.assertEqual(len(times), 2)
        self.assertGreaterEqual(times[1] - times[0], 0.3)
        for request in session.requests:
            self.assertIn("tool=vbio-copilot", request["url"])

    def test_ncbi_identification_params_when_configured(self):
        # With email + api_key configured, every E-utility URL carries tool/email/api_key
        # (NBK25497: identification lets NCBI contact the operator instead of blocking).
        session = FakeHttpSession([
            FakeHttpResponse(payload={"esearchresult": {"idlist": []}}),
        ])
        OnlineDatabaseSkills(
            session=session, timeout_seconds=3,
            ncbi_email="ops@example.org", ncbi_api_key="KEY123",
        ).execute("pubmed.search", {"query": "x"})
        url = session.requests[0]["url"]
        self.assertIn("tool=vbio-copilot", url)
        self.assertIn("email=ops%40example.org", url)
        self.assertIn("api_key=KEY123", url)

    def test_ncbi_rate_relaxes_with_an_api_key(self):
        skills = OnlineDatabaseSkills(session=FakeHttpSession([]), timeout_seconds=3)
        self.assertAlmostEqual(skills._host_rate_interval("eutils.ncbi.nlm.nih.gov"), 0.34, places=2)
        keyed = OnlineDatabaseSkills(
            session=FakeHttpSession([]), timeout_seconds=3, ncbi_api_key="KEY123")
        self.assertAlmostEqual(keyed._host_rate_interval("eutils.ncbi.nlm.nih.gov"), 0.11, places=2)

    def test_user_agent_carries_contact_email_when_configured(self):
        # UniProt's programmatic-access help asks for a contact email inside the User-Agent so
        # they can reach the operator before blocking.
        skills = OnlineDatabaseSkills(session=FakeHttpSession([]), timeout_seconds=3)
        self.assertNotIn("contact:", skills._http_user_agent())
        with_email = OnlineDatabaseSkills(
            session=FakeHttpSession([]), timeout_seconds=3, contact_email="ops@example.org")
        self.assertIn("contact: ops@example.org", with_email._http_user_agent())


class ThrottleAndRetryAfterTests(unittest.TestCase):
    """429/Retry-After handling per RFC 9110 + urllib3's RETRY_AFTER_STATUS_CODES convention."""

    @patch("management_api.copilot_skills.online_databases.time.sleep")
    def test_429_with_retry_after_is_retried_honoring_the_header(self, sleep_mock):
        # RCSB documents HTTP 429 for rate limiting and recommends backoff; a server that sends
        # Retry-After must be waited for exactly (capped), then the retry succeeds.
        session = FakeHttpSession([
            FakeHttpResponse(ok=False, status_code=429, text="rate limited", headers={"Retry-After": "1"}),
            FakeHttpResponse(payload={"results": []}),
        ])
        result = OnlineDatabaseSkills(session=session, timeout_seconds=3).execute(
            "uniprot.search", {"query": "gene:DHODH"}
        )
        self.assertEqual(result["count"], 0)
        self.assertEqual(len(session.requests), 2)
        # The server-demanded 1-second wait was honored exactly (not jitter-replaced).
        retry_waits = [call.args[0] for call in sleep_mock.call_args_list if call.args]
        self.assertIn(1.0, retry_waits)

    @patch("management_api.copilot_skills.online_databases.time.sleep")
    def test_429_without_retry_after_uses_jittered_backoff(self, sleep_mock):
        session = FakeHttpSession([
            FakeHttpResponse(ok=False, status_code=429, text="rate limited"),
            FakeHttpResponse(payload={"results": []}),
        ])
        result = OnlineDatabaseSkills(session=session, timeout_seconds=3).execute(
            "uniprot.search", {"query": "gene:DHODH"}
        )
        self.assertEqual(result["count"], 0)
        self.assertEqual(len(session.requests), 2)
        # Jittered backoff happened: some wait falls within [0.5*base, 1.5*base] of the
        # configured base (rate-limit spacing sleeps may also appear among the waits).
        base = OnlineDatabaseSkills._RETRY_BACKOFF_SECONDS
        waits = [call.args[0] for call in sleep_mock.call_args_list if call.args]
        self.assertTrue(
            any(base * 0.5 - 1e-9 <= wait <= base * 1.5 + 1e-9 for wait in waits),
            f"no jittered backoff wait within [{base*0.5}, {base*1.5}]: {waits}",
        )

    @patch("management_api.copilot_skills.online_databases.time.sleep")
    def test_retry_after_is_capped_to_the_turn_patience(self, sleep_mock):
        # A server demanding an hour must not hang a copilot turn: the honored wait is capped.
        session = FakeHttpSession([
            FakeHttpResponse(ok=False, status_code=503, text="busy", headers={"Retry-After": "3600"}),
            FakeHttpResponse(payload={"results": []}),
        ])
        result = OnlineDatabaseSkills(session=session, timeout_seconds=3).execute(
            "uniprot.search", {"query": "gene:DHODH"}
        )
        self.assertEqual(result["count"], 0)
        waits = [call.args[0] for call in sleep_mock.call_args_list if call.args]
        self.assertIn(min(3600.0, OnlineDatabaseSkills._RETRY_AFTER_CAP_SECONDS), waits)

    def test_parse_retry_after_accepts_both_rfc_formats(self):
        parse = OnlineDatabaseSkills._parse_retry_after
        self.assertEqual(parse("2"), 2.0)
        self.assertEqual(parse("0"), 0.0)
        self.assertIsNone(parse(""))
        self.assertIsNone(parse("garbage"))
        http_date = email.utils.formatdate(time.time() + 3.0, usegmt=True)
        delay = parse(http_date)
        self.assertIsNotNone(delay)
        self.assertGreaterEqual(delay, 0.0)
        self.assertLessEqual(delay, 5.0)

    @patch("management_api.copilot_skills.online_databases.time.sleep")
    def test_408_and_502_are_retryable(self, _sleep):
        # 408 (client may repeat) and 502 (ChEMBL's documented proxy flakiness) both retry.
        session = FakeHttpSession([
            FakeHttpResponse(ok=False, status_code=408, text="timeout"),
            FakeHttpResponse(ok=False, status_code=502, text="bad gateway"),
            FakeHttpResponse(payload={"results": []}),
        ])
        result = OnlineDatabaseSkills(session=session, timeout_seconds=3).execute(
            "uniprot.search", {"query": "gene:DHODH"}
        )
        self.assertEqual(result["count"], 0)
        self.assertEqual(len(session.requests), 3)


class RcsbBatchTests(unittest.TestCase):
    """Entry summaries are fetched one id per request.

    The Data API's core/entry endpoint accepts exactly ONE id (comma-joined ids answer 404 —
    live-verified against data.rcsb.org); a previous 'batched' call was a guaranteed 404 that
    the caller silently fell back from, costing an extra request per search. These tests pin
    the real per-entry behavior and its failure tolerance."""

    @staticmethod
    def _search_payload(ids: List[str]) -> FakeHttpResponse:
        return FakeHttpResponse(payload={
            "result_set": [{"identifier": pdb_id, "score": 1.0 - i * 0.1} for i, pdb_id in enumerate(ids)],
        })

    @patch("management_api.copilot_skills.online_databases.time.sleep")
    def test_search_fetches_one_summary_per_entry(self, _sleep):
        session = FakeHttpSession([
            self._search_payload(["1UBQ", "1IVO"]),
            FakeHttpResponse(payload={"struct": {"title": "Ubiquitin"}, "rcsb_entry_info": {}}),
            FakeHttpResponse(payload={"struct": {"title": "IgG"}, "rcsb_entry_info": {}}),
        ])
        result = OnlineDatabaseSkills(session=session, timeout_seconds=3).execute(
            "rcsb.search", {"text": "ubiquitin", "size": 2}
        )
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["results"][0]["title"], "Ubiquitin")
        self.assertEqual(result["results"][0]["score"], 1.0)
        data_calls = [r for r in session.requests if "data.rcsb.org" in r["url"]]
        self.assertEqual(len(data_calls), 2)
        self.assertNotIn(",", data_calls[0]["url"])

    @patch("management_api.copilot_skills.online_databases.time.sleep")
    def test_single_entry_failure_degrades_without_killing_the_search(self, _sleep):
        # One entry's summary stays down (retries exhausted); the other still fills results.
        session = FakeHttpSession(
            [self._search_payload(["1UBQ", "1IVO"])]
            + [FakeHttpResponse(ok=False, status_code=500, text="down")]
            * OnlineDatabaseSkills._RETRY_MAX_ATTEMPTS
            + [FakeHttpResponse(payload={"struct": {"title": "IgG"}, "rcsb_entry_info": {}})]
        )
        result = OnlineDatabaseSkills(session=session, timeout_seconds=3).execute(
            "rcsb.search", {"text": "ubiquitin", "size": 2}
        )
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["title"], "IgG")


# --------------------------------------------------------------------------- #
# Planner-loop regressions: the model drives search -> resolve; skill handlers
# are stubbed so no network is needed.
# --------------------------------------------------------------------------- #


class SequenceModelSession:
    def __init__(self, responses: List[str]) -> None:
        self.responses = list(responses)
        self.requests: List[Dict[str, Any]] = []

    def post(self, url: str, headers: Dict[str, str], json: Dict[str, Any], timeout: float, **kwargs: Any) -> FakeResponse:
        self.requests.append({"url": url, "json": json})
        if not self.responses:
            if is_phase2_request(json):
                return FakeResponse("")
            raise AssertionError("unexpected model request")
        return FakeResponse(self.responses.pop(0))


def _make_assistant(responses: List[Dict[str, Any]]) -> tuple[CopilotAssistant, SequenceModelSession]:
    session = SequenceModelSession([json.dumps(response) for response in responses])
    assistant = CopilotAssistant(
        chat_api_url="http://model.invalid/v1/chat/completions",
        chat_api_key="test-key",
        chat_model="test-model",
        timeout_seconds=3,
        session=session,
        logger=NullLogger(),
        max_planner_rounds=6,
    )
    return assistant, session


def _harness(handlers: Dict[str, tuple[OnlineSkillDefinition, Any]]) -> CopilotSkillHarness:
    skills = OnlineDatabaseSkills(session=SequenceModelSession([]), timeout_seconds=3)
    for definition, handler in handlers.values():
        skills.register(definition, handler)
    return CopilotSkillHarness(skills=skills, max_workers=4)


def _all_message_contents(request_payload: Dict[str, Any]) -> str:
    return "\n".join(str(message.get("content") or "") for message in request_payload["json"]["messages"])


_PUBCHEM_SCHEMA = {
    "type": "object",
    "properties": {
        "identifier": {"type": "string", "minLength": 1},
        "namespace": {"type": "string", "enum": ["name", "cid", "smiles", "inchi", "inchikey"]},
    },
    "required": ["identifier"],
    "additionalProperties": False,
}
_QUERY_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string", "minLength": 1}, "size": {"type": "integer", "minimum": 1, "maximum": 10}},
    "required": ["query"],
    "additionalProperties": False,
}
_ID_SCHEMA = {"type": "object", "properties": {"identifier": {"type": "string", "minLength": 1}}, "required": ["identifier"], "additionalProperties": False}
_TARGET_ACTIVITY_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1},
        "accession": {"type": "string", "minLength": 1},
        "target_chembl_id": {"type": "string", "minLength": 1},
        "organism": {"type": "string", "minLength": 1},
        "activity_type": {"type": "string", "minLength": 1},
        "size": {"type": "integer", "minimum": 1, "maximum": 10},
    },
    "required": ["query"],
    "additionalProperties": False,
}


class PlannerLoopRegressionTests(unittest.TestCase):
    def test_compound_code_resolved_by_name_search_not_fabricated_cid(self):
        searched: Dict[str, Any] = {}

        def pubchem_search(arguments: Dict[str, Any]) -> Dict[str, Any]:
            searched["identifier"] = arguments.get("identifier")
            searched["namespace"] = arguments.get("namespace")
            return {
                "source": "pubchem",
                "query": arguments.get("identifier"),
                "count": 1,
                "results": [
                    {
                        "cid": "134611040",
                        "title": "benzimidazole-6-carboxylic acid",
                        "smiles": "C1CO[C@@H]1CN2",
                        "sourceUrl": "https://pubchem.ncbi.nlm.nih.gov/compound/134611040",
                    }
                ],
            }

        responses = [
            {
                "message": "Looking up the compound by name.",
                "questions": [],
                "operations": [{"id": "find", "skill": "pubchem.search", "arguments": {"identifier": "PF06882961", "namespace": "name"}, "depends_on": []}],
            },
            {"message": "PF06882961 -> CID 134611040, SMILES C1CO[C@@H]1CN2.", "questions": [], "operations": []},
        ]
        assistant, session = _make_assistant(responses)
        assistant.skill_harness = _harness({"pubchem.search": (OnlineSkillDefinition(name="pubchem.search", description="stub", input_schema=_PUBCHEM_SCHEMA), pubchem_search)})

        result = assistant.plan_turn(
            context_type="workspace", context_payload={}, user_id="u", username="alice", content="PF06882961 的 SMILES 是什么"
        )

        # the planner searched by the exact identifier the user gave (name namespace), never a fabricated numeric CID
        self.assertEqual(searched["identifier"], "PF06882961")
        self.assertEqual(searched["namespace"], "name")
        feedback = _all_message_contents(session.requests[1])
        self.assertIn("134611040", feedback)
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["actions"], [])

    def test_gene_name_resolved_via_search_then_resolve(self):
        resolved: List[str] = []

        def uniprot_search(arguments: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "source": "uniprot",
                "query": arguments.get("query"),
                "count": 1,
                "results": [
                    {
                        "accession": "Q02127",
                        "entryName": "PYRD_HUMAN",
                        "geneNames": "DHODH",
                        "proteinName": "Dihydroorotate dehydrogenase",
                        "organism": "Homo sapiens",
                        "length": 395,
                        "reviewed": True,
                        "sourceUrl": "https://www.uniprot.org/uniprotkb/Q02127/entry",
                    }
                ],
            }

        def uniprot_resolve(arguments: Dict[str, Any]) -> Dict[str, Any]:
            resolved.append(arguments.get("identifier"))
            return {"source": "uniprot", "identifier": "Q02127", "accession": "Q02127", "label": "DHODH", "sequence": "MTPRKRGTG..."}

        responses = [
            {
                "message": "Searching for the target.",
                "questions": [],
                "operations": [{"id": "find", "skill": "uniprot.search", "arguments": {"query": "gene:DHODH AND organism_id:9606"}, "depends_on": []}],
            },
            {
                "message": "Fetching the sequence.",
                "questions": [],
                "operations": [{"id": "seq", "skill": "uniprot.resolve", "arguments": {"identifier": "Q02127"}, "depends_on": ["find"]}],
            },
            {"message": "人源 DHODH (Q02127) 序列已获取。", "questions": [], "operations": []},
        ]
        assistant, session = _make_assistant(responses)
        assistant.skill_harness = _harness(
            {
                "uniprot.search": (OnlineSkillDefinition(name="uniprot.search", description="stub", input_schema=_QUERY_SCHEMA), uniprot_search),
                "uniprot.resolve": (OnlineSkillDefinition(name="uniprot.resolve", description="stub", input_schema=_ID_SCHEMA), uniprot_resolve),
            }
        )

        result = assistant.plan_turn(
            context_type="workspace", context_payload={}, user_id="u", username="alice", content="人的 DHODH 的序列"
        )

        # the accession surfaced by search was the one resolved for the sequence
        self.assertEqual(resolved, ["Q02127"])
        self.assertEqual(result["state"], "complete")
        self.assertIn("MTPRKRGTG", _all_message_contents(session.requests[2]))

    def test_no_match_reported_honestly_without_substitution(self):
        def pubchem_search(arguments: Dict[str, Any]) -> Dict[str, Any]:
            return {"source": "pubchem", "query": arguments.get("identifier"), "count": 0, "results": []}

        responses = [
            {
                "message": "Searching the compound.",
                "questions": [],
                "operations": [{"id": "find", "skill": "pubchem.search", "arguments": {"identifier": "NOPE9999", "namespace": "name"}, "depends_on": []}],
            },
            {"message": "No authoritative record was found for NOPE9999.", "questions": [], "operations": []},
        ]
        assistant, _session = _make_assistant(responses)
        assistant.skill_harness = _harness({"pubchem.search": (OnlineSkillDefinition(name="pubchem.search", description="stub", input_schema=_PUBCHEM_SCHEMA), pubchem_search)})

        result = assistant.plan_turn(
            context_type="workspace", context_payload={}, user_id="u", username="alice", content="NOPE9999 的 SMILES"
        )
        # no fabricated operation; an honest message reaches the user
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["actions"], [])
        self.assertIn("NOPE9999", result["content"])

    def test_target_inhibitor_question_routes_to_target_activity_skill(self):
        # the inverse bioactivity question ("find inhibitors for target X") routes to
        # chembl.target_activity; the planner resolves the gene to an accession and the
        # returned compounds + potency reach the user.
        captured: Dict[str, Any] = {}

        def chembl_target_activity(arguments: Dict[str, Any]) -> Dict[str, Any]:
            captured["accession"] = arguments.get("accession")
            captured["query"] = arguments.get("query")
            return {
                "source": "chembl",
                "query": arguments.get("query"),
                "target": {"chemblId": "CHEMBL4618", "name": "MAP4K1", "organism": "Homo sapiens", "type": "SINGLE PROTEIN"},
                "count": 1,
                # flat record (matches the real skill shape) so the summarizer surfaces smiles + value
                "results": [
                    {
                        "title": "inhibitor X",
                        "chemblId": "CHEMBL123",
                        "smiles": "CCO",
                        "activityType": "IC50",
                        "value": 12.0,
                        "units": "nM",
                    }
                ],
            }

        responses = [
            {
                "message": "Resolving the target and searching ChEMBL for inhibitors.",
                "questions": [],
                "operations": [
                    {"id": "inh", "skill": "chembl.target_activity", "arguments": {"query": "HPK1", "accession": "Q92918"}, "depends_on": []}
                ],
            },
            {"message": "Found inhibitor X (IC50 12 nM) for HPK1.", "questions": [], "operations": []},
        ]
        assistant, session = _make_assistant(responses)
        assistant.skill_harness = _harness(
            {"chembl.target_activity": (OnlineSkillDefinition(name="chembl.target_activity", description="stub", input_schema=_TARGET_ACTIVITY_SCHEMA), chembl_target_activity)}
        )

        result = assistant.plan_turn(
            context_type="workspace", context_payload={}, user_id="u", username="alice", content="HPK1的抑制剂给我找几个"
        )

        # the skill received the resolved accession, and both the potency AND the SMILES reached
        # the next planner round (flat records surface smiles/value to the summarizer)
        round2_input = _all_message_contents(session.requests[1])
        self.assertEqual(captured["accession"], "Q92918")
        self.assertEqual(captured["query"], "HPK1")
        self.assertEqual(result["state"], "complete")
        self.assertIn("12", round2_input)   # potency value
        self.assertIn("CCO", round2_input)  # SMILES — the field that was previously hidden

    def test_multi_step_lookup_then_action_consumes_observed_scalar(self):        # round 1: read-only lookup; round 2: a confirmation action that consumes a scalar field
        # from the lookup via $fromObservation — the query->action composition path.
        def source_read(_: Dict[str, Any]) -> Dict[str, Any]:
            return {"value": "PROTEIN_SEQUENCE_FROM_OBSERVATION"}

        read_def = OnlineSkillDefinition(
            name="source:read",
            description="Read one authoritative value.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        )
        build_def = CopilotSkillDefinition(
            name="host:build",
            label="Build task",
            description="Build a task from a retrieved value.",
            input_schema={"type": "object", "properties": {"value": {"type": "string", "minLength": 1}}, "required": ["value"], "additionalProperties": False},
            effect="create",
        )

        responses = [
            {
                "message": "Looking up the sequence.",
                "questions": [],
                "operations": [{"id": "read", "skill": "source:read", "arguments": {}, "depends_on": []}],
            },
            {
                "message": "Creating the task from the retrieved sequence.",
                "questions": [],
                "operations": [
                    {
                        "id": "build",
                        "skill": "host:build",
                        "arguments": {"value": {"$fromObservation": "read", "field": "value", "index": 0}},
                        "depends_on": ["read"],
                    }
                ],
            },
        ]
        assistant, _session = _make_assistant(responses)
        assistant.skill_harness = _harness({read_def.name: (read_def, source_read)})
        from unittest.mock import patch
        with patch("management_api.copilot.build_cross_context_skill_definitions", return_value=[build_def]):
            result = assistant.plan_turn(
                context_type="workspace", context_payload={}, user_id="u", username="alice", content="查序列然后建任务"
            )

        self.assertEqual(result["state"], "await_confirmation")
        self.assertEqual(len(result["actions"]), 1)
        # the confirmation action carries the scalar materialized from the read observation
        self.assertEqual(result["actions"][0]["arguments"]["value"], "PROTEIN_SEQUENCE_FROM_OBSERVATION")
        self.assertTrue(result["actions"][0]["needs_confirmation"])


if __name__ == "__main__":
    unittest.main()


class TranslationSkillTests(unittest.TestCase):
    """translate.to_english is a planner-executed atomic conversion: the model supplies the English
    form itself; the operation records the conversion as an auditable observation for the next
    lookup to consume. No network, no extra model call."""

    def _skills(self):
        from management_api.copilot_skills.translation import register_translation_skills
        skills = OnlineDatabaseSkills(session=requests.Session())
        register_translation_skills(skills)
        return skills

    def test_records_the_planner_provided_conversion(self):
        skills = self._skills()
        result = skills.execute("translate.to_english", {"text": "布洛芬", "english": "ibuprofen", "domain": "compound"})
        self.assertEqual(result["source"], "translate")
        self.assertEqual(result["count"], 1)
        record = result["results"][0]
        self.assertEqual(record["original"], "布洛芬")
        self.assertEqual(record["english"], "ibuprofen")
        self.assertEqual(record["domain"], "compound")

    def test_missing_english_form_is_a_contract_error(self):
        skills = self._skills()
        with self.assertRaises(ValueError):
            skills.execute("translate.to_english", {"text": "布洛芬"})

    def test_missing_text_is_a_contract_error(self):
        skills = self._skills()
        with self.assertRaises(ValueError):
            skills.execute("translate.to_english", {"english": "ibuprofen"})

    def test_skill_is_registered_and_schema_audited(self):
        skills = self._skills()
        names = [d.name for d in skills.definitions]
        self.assertIn("translate.to_english", names)
        definition = next(d for d in skills.definitions if d.name == "translate.to_english")
        self.assertEqual(definition.input_schema["required"], ["text", "english"])


class PotencyRankingTests(unittest.TestCase):
    """Unit-aware potency ranking: ChEMBL streams nM / uM / ug.mL-1 mixed; ranking on the raw
    number put 0.2 ug.mL-1 between sub-nM rows (live-verified). Rank on the nM-normalized
    value; non-molar units keep their row but rank last."""

    def test_rank_normalizes_molar_units(self):
        rank = OnlineDatabaseSkills._potency_rank_nm
        self.assertEqual(rank(0.2, "nM"), 0.2)
        self.assertEqual(rank(0.2, "uM"), 200.0)
        self.assertEqual(rank(200.0, "pM"), 0.2)
        self.assertEqual(rank(0.001, "mM"), 1000.0)
        self.assertEqual(rank(0.2, "ug.mL-1"), float("inf"))

    def test_target_activity_orders_by_normalized_potency(self):
        # 0.2 ug.mL-1 (raw 0.2) must NOT outrank 5 nM (raw 5): nM-normalized ordering wins.
        # Direct-id lookup answers a bare target object (not the {"targets": [...]} list shape).
        target_payload = {"target_chembl_id": "CHEMBL_T", "pref_name": "TT", "target_type": "SINGLE PROTEIN"}
        activity_payload = {"activities": [
            {"molecule_chembl_id": "CHEMBL1", "standard_type": "IC50", "standard_value": "0.2",
             "standard_units": "ug.mL-1", "target_pref_name": "", "target_chembl_id": "CHEMBL_T"},
            {"molecule_chembl_id": "CHEMBL1", "standard_type": "Ki", "standard_value": "5",
             "standard_units": "nM", "target_pref_name": "", "target_chembl_id": "CHEMBL_T"},
        ]}
        molecules_payload = {"molecules": [{"molecule_chembl_id": "CHEMBL1", "pref_name": "X"}]}
        session = FakeHttpSession([
            FakeHttpResponse(payload=target_payload),
            FakeHttpResponse(payload=activity_payload),
            FakeHttpResponse(payload=molecules_payload),
        ])
        result = OnlineDatabaseSkills(session=session, timeout_seconds=3).execute(
            "chembl.target_activity", {"query": "X", "target_chembl_id": "CHEMBL_T", "size": 1})
        record = result["results"][0]
        self.assertEqual(record["units"], "nM")  # the 5 nM Ki is the best (0.2 ug.mL-1 ranked last)
        self.assertEqual(record["value"], 5.0)


class ChemblTargetResolutionTests(unittest.TestCase):
    """Accession checked FIRST (as the planner-facing description promises), and a nonexistent
    target_chembl_id is an authoritative NO_MATCH — not a source failure."""

    def test_unknown_chembl_target_id_is_no_match(self):
        session = FakeHttpSession([
            FakeHttpResponse(payload={"molecules": [{"molecule_chembl_id": "CHEMBL1"}]}),
            FakeHttpResponse(ok=False, status_code=404, text=""),
            FakeHttpResponse(payload={"molecules": []}),
        ])
        result = OnlineDatabaseSkills(session=session, timeout_seconds=3).execute(
            "chembl.target_activity", {"query": "X", "target_chembl_id": "CHEMBL99999999"})
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["results"], [])
        from management_api.copilot_skill_harness import CopilotSkillHarness
        self.assertEqual(
            CopilotSkillHarness.classify_observation({"ok": True, "values": [result]}), "NO_MATCH"
        )
