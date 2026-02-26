import streamlit as st

from frontend.views.designer_page import render_designer_page
from frontend.views.bicyclic_designer_page import render_bicyclic_designer_page


def _default_peptide_mode() -> str:
    task_type_raw = st.query_params.get("task_type", "peptide_designer")
    if isinstance(task_type_raw, list):
        task_type_raw = task_type_raw[0] if task_type_raw else "peptide_designer"
    task_type = str(task_type_raw).strip().lower()

    if task_type == "bicyclic_designer":
        return "bicyclic"
    if task_type == "designer":
        return "cyclic"
    if st.session_state.get("bicyclic_task_id"):
        return "bicyclic"
    return "cyclic"


def render_peptide_designer_page():
    task_type_raw = st.query_params.get("task_type", "peptide_designer")
    if isinstance(task_type_raw, list):
        task_type_raw = task_type_raw[0] if task_type_raw else "peptide_designer"
    task_type = str(task_type_raw).strip().lower()

    preferred_mode = _default_peptide_mode()
    if task_type in {"designer", "bicyclic_designer"}:
        st.session_state.peptide_design_mode = preferred_mode
    elif "peptide_design_mode" not in st.session_state:
        st.session_state.peptide_design_mode = preferred_mode

    st.markdown("### 🧪 多肽设计")
    design_mode = st.radio(
        "选择设计模式",
        options=["cyclic", "bicyclic"],
        format_func=lambda x: "⭕ 环肽设计" if x == "cyclic" else "🔗 双环肽设计",
        horizontal=True,
        key="peptide_design_mode",
        help="在同一入口中切换环肽与双环肽设计。"
    )
    st.markdown("---")

    if design_mode == "cyclic":
        render_designer_page(allow_glycopeptide=False)
    else:
        render_bicyclic_designer_page()
