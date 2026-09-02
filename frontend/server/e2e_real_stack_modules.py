"""Module-coverage real-stack suite: every lookup skill family and every host-action family
exercised end-to-end against the deployed API with the REAL LLM and REAL sources.

Coverage matrix (one turn each unless noted):
  lookup families   pubchem / rcsb / uniprot / alphafold / chembl(+compute) / pubmed /
                    clinicaltrials / translate
  host actions      tasks:update_view (+tasks:failure) / tasks:rename / tasks:open /
                    tasks:copy / tasks:create_with_sequence / tasks:create_virtual_screening /
                    task_detail:apply_parameter_patch / apply_metadata_patch
  data-value honesty  every answer's PDB ids, accessions, SMILES and sequences trace to
                    retrieval (the identifier audit enforces this; failures assert it)
"""
from __future__ import annotations

import sys
import time

from e2e_real_stack import (
    RealStackSession,
    TASK_LIST_AFFINITY,
    TASK_LIST_WITH_ROWS,
    TASK_DETAIL_PREDICTION,
    has_action_skill,
    has_choice_question,
    msg_contains,
    obs_source,
    question_options_match,
)

TASK_LIST_VS = {
    "page": {"contextType": "task_list", "workflowKey": "virtual_screening", "workflowTitle": "Virtual Screening"},
    "project": {"id": "e2e-vs", "name": "E2E VS", "task_type": "virtual_screening", "workflow_key": "virtual_screening"},
    "rows": [],
    "summary": {"total": 0, "all": 0},
}

TASK_DETAIL_PEPTIDE = {
    "page": {"contextType": "task_detail", "workflowKey": "peptide_design", "workflowTitle": "Peptide Design", "runLabel": "Run Design"},
    "project": {"id": "e2e-pd", "name": "E2E Peptide", "task_type": "peptide_design", "workflow_key": "peptide_design"},
    "draft": {
        "taskName": "New design", "taskSummary": "", "backend": "boltz",
        "options": {"seed": 7, "peptideBinderLength": 12},
        "components": [{"id": "c1", "type": "protein", "label": "Target", "sequence": "MKTV"}],
        "constraints": [],
    },
    "runtime": {"displayTaskState": "DRAFT", "runDisabled": False, "runBlockedReason": "", "activeTaskId": ""},
    "currentTask": None,
}


def run_all():
    started = time.time()
    results = []

    def one(name, ctx, text, expects):
        s = RealStackSession(name, ctx[0], ctx[1])
        s.turn(text, expect=expects)
        results.extend(s.results)
        return s

    # ── lookup families ─────────────────────────────────────────────────────────────
    one("mod-pubchem", ("project_list", {"page": {"contextType": "project_list"}, "summary": {"all": 2}}),
        "查一下阿司匹林的分子式和SMILES",
        [("pubchem", obs_source("pubchem")), ("cites retrieved identity", msg_contains(r"CID|aspirin|阿司匹林"))])

    one("mod-rcsb", ("task_detail", TASK_LIST_AFFINITY and {"page": {"contextType": "task_detail", "workflowKey": "affinity"},
        "project": {"id": "e2e-1", "task_type": "affinity", "workflow_key": "affinity"},
        "draft": {"taskName": "T", "backend": "boltz", "options": {"affinityMode": "dock"}, "components": [], "constraints": []},
        "runtime": {"displayTaskState": "DRAFT", "runDisabled": True, "runBlockedReason": "Upload target structure first.", "activeTaskId": ""},
        "currentTask": None}),
        "帮我检索人源 DHODH 的X射线结构",
        [("rcsb", obs_source("rcsb"))])

    one("mod-uniprot", ("task_detail", TASK_DETAIL_PREDICTION),
        "查一下人源 BTK 的 UniProt accession 和序列长度",
        [("uniprot", obs_source("uniprot"))])

    one("mod-alphafold", ("task_detail", TASK_DETAIL_PREDICTION),
        "给我人源 DHODH 的 AlphaFold 模型的 pLDDT 信息",
        [("alphafold or asks identity", lambda r: obs_source("alphafold")(r) or bool(r.get("questions")) or obs_source("uniprot")(r))])

    one("mod-chembl", ("project_list", {"page": {"contextType": "project_list"}, "summary": {"all": 2}}),
        "查来那度胺（lenalidomide）在 ChEMBL 的活性靶点",
        [("chembl or clarifies", lambda r: obs_source("chembl")(r) or bool(r.get("questions")) or obs_source("translate")(r))])

    one("mod-pubmed", ("project_list", {"page": {"contextType": "project_list"}, "summary": {"all": 2}}),
        "搜一下 DHODH 抑制剂相关的近期文献",
        [("pubmed", obs_source("pubmed"))])

    one("mod-clinicaltrials", ("project_list", {"page": {"contextType": "project_list"}, "summary": {"all": 2}}),
        "有关于 DHODH 抑制的临床试验吗",
        [("clinicaltrials", obs_source("clinicaltrials"))])

    # ── host actions ────────────────────────────────────────────────────────────────
    one("host-failure-filter", ("task_list", TASK_LIST_WITH_ROWS),
        "只显示失败的任务",
        [("filter", lambda r: has_action_skill("tasks:failure")(r) or has_action_skill("tasks:update_view")(r))])

    one("host-rename", ("task_list", TASK_LIST_WITH_ROWS),
        "把 KLK screen 改名为 KLK screen v2",
        [("rename", lambda r: has_action_skill("tasks:rename")(r))])

    one("host-open", ("task_list", TASK_LIST_WITH_ROWS),
        "打开失败的那个任务看看",
        [("open", lambda r: has_action_skill("tasks:open")(r))])

    one("host-copy", ("task_list", TASK_LIST_WITH_ROWS),
        "复制 Lysozyme fold 那个任务做新草稿",
        [("copy", lambda r: has_action_skill("tasks:copy")(r))])

    one("host-create-seq", ("task_list", {
        "page": {"contextType": "task_list", "workflowKey": "prediction", "workflowTitle": "Prediction"},
        "project": {"id": "e2e-2", "task_type": "prediction", "workflow_key": "prediction"},
        "rows": [], "summary": {"total": 0, "all": 0}}),
        "帮我检索人源溶菌酶的序列并创建预测任务",
        [("uniprot first", lambda r: obs_source("uniprot")(r) or has_choice_question(r)),
         ("create with sequence", lambda r: has_action_skill("tasks:create_with_sequence")(r) or bool(r.get("questions")))])

    one("host-vs-create", ("task_list", TASK_LIST_VS),
        "创建一个虚拟筛选任务，靶点是人的 DHODH，筛选阿司匹林和布洛芬两个分子",
        [("create VS", lambda r: has_action_skill("tasks:create_virtual_screening")(r) or bool(r.get("questions")))])

    one("host-param-patch", ("task_detail", TASK_DETAIL_PEPTIDE),
        "把 binder 长度改成 15，种子改成 42",
        [("patch", lambda r: has_action_skill("task_detail:apply_parameter_patch")(r))])

    one("host-metadata", ("task_detail", TASK_DETAIL_PEPTIDE),
        "任务名改成 PD-run1，描述写 DHODH binder 设计",
        [("metadata", lambda r: has_action_skill("task_detail:apply_metadata_patch")(r))])

    passed = sum(1 for r in results if r["ok"])
    print("\n" + "=" * 70)
    print(f"MODULE REAL-STACK: {passed}/{len(results)} turns passed ({time.time() - started:.0f}s)")
    for r in results:
        mark = "PASS" if r["ok"] else "FAIL"
        print(f"  [{mark}] {str(r['label'])[:58]:60} state={r.get('state')}{' | ' + '; '.join(r['failures']) if not r['ok'] else ''}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(run_all())
