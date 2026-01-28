import json
import math
import time

import numpy as np
import requests
import streamlit as st

from frontend.prediction_client import (
    predict_boltz2score,
    get_status,
    download_and_process_results,
)
from frontend.url_state import URLStateManager
from frontend.utils import (
    get_chain_ids_from_structure,
    read_cif_from_string,
    extract_protein_residue_bfactors,
    visualize_structure_py3dmol,
)


def _format_metric_value(value, precision: int = 2) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, (int, float)):
        return f"{value:.{precision}f}"
    return str(value)


def _pair_iptm_rows(confidence_data: dict, chain_map: dict) -> list[dict]:
    pair_rows = []
    pair_map = confidence_data.get("pair_chains_iptm")
    if not isinstance(pair_map, dict):
        return pair_rows

    seen_pairs = set()
    for chain_a, chain_b_map in pair_map.items():
        if not isinstance(chain_b_map, dict):
            continue
        for chain_b, value in chain_b_map.items():
            if chain_a == chain_b or not isinstance(value, (int, float)):
                continue
            pair_key = tuple(sorted((str(chain_a), str(chain_b))))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            label_a = chain_map.get(str(chain_a), str(chain_a))
            label_b = chain_map.get(str(chain_b), str(chain_b))
            pair_rows.append({
                "chain_a": label_a,
                "chain_b": label_b,
                "pair_ipTM": float(value),
            })

    return sorted(pair_rows, key=lambda row: row["pair_ipTM"], reverse=True)


def render_affinity_page():
    URLStateManager.restore_state_from_url()

    st.markdown("### 🔬 结构置信度 & 亲和力预测")
    st.markdown("上传 PDB/CIF 结构文件，直接输出置信度；若指定配体链，则额外预测亲和力。")

    if 'affinity_task_id' not in st.session_state:
        st.session_state.affinity_task_id = None
    if 'affinity_results' not in st.session_state:
        st.session_state.affinity_results = None
    if 'affinity_error' not in st.session_state:
        st.session_state.affinity_error = None
    if 'affinity_cif' not in st.session_state:
        st.session_state.affinity_cif = None

    is_running = (
        st.session_state.affinity_task_id is not None
        and st.session_state.affinity_results is None
        and st.session_state.affinity_error is None
    )

    with st.expander("📤 上传结构文件", expanded=not is_running and st.session_state.affinity_results is None):
        uploaded_file = st.file_uploader(
            "选择结构文件 (PDB/CIF)",
            type=['pdb', 'cif'],
            disabled=is_running
        )

        target_chain_str = ""
        ligand_chain_str = ""
        chain_info = {"all_chains": [], "polymer_chains": [], "ligand_chains": []}

        if uploaded_file is not None and not is_running:
            file_content = uploaded_file.getvalue().decode("utf-8")
            chain_info = get_chain_ids_from_structure(file_content, uploaded_file.name)

            all_chains = chain_info.get("all_chains", [])
            protein_chains = chain_info.get("polymer_chains", [])
            ligand_chains = chain_info.get("ligand_chains", [])

            st.caption(
                f"检测到链：{', '.join(all_chains) if all_chains else '未检测到'}"
            )

            col1, col2 = st.columns(2)
            with col1:
                target_selected = st.multiselect(
                    "Target 链 (蛋白质)",
                    options=protein_chains or all_chains,
                    default=protein_chains,
                    disabled=is_running,
                )
            with col2:
                ligand_selected = st.multiselect(
                    "Ligand 链 (可选)",
                    options=ligand_chains or all_chains,
                    default=ligand_chains,
                    disabled=is_running,
                )

            target_chain_str = ",".join(target_selected)
            ligand_chain_str = ",".join(ligand_selected)

            with st.expander("手动输入链 (可选)", expanded=False):
                manual_target = st.text_input(
                    "Target 链 (逗号分隔)",
                    value=target_chain_str,
                    disabled=is_running,
                    help="当自动检测不准确时，可手动输入链 ID，如 A,B"
                )
                manual_ligand = st.text_input(
                    "Ligand 链 (逗号分隔)",
                    value=ligand_chain_str,
                    disabled=is_running,
                    help="如需亲和力预测，请填写配体链 ID"
                )
                if manual_target.strip():
                    target_chain_str = manual_target.strip()
                if manual_ligand.strip():
                    ligand_chain_str = manual_ligand.strip()

        files_ready = uploaded_file is not None
        if files_ready and ligand_chain_str and not target_chain_str:
            st.warning("已指定配体链，但未指定 target 链；请补充 target 链或清空配体链。")
            files_ready = False

        if st.button("🚀 开始预测", key="start_affinity", type="primary", disabled=is_running or not files_ready, use_container_width=True):
            st.session_state.affinity_task_id = None
            st.session_state.affinity_results = None
            st.session_state.affinity_error = None
            st.session_state.affinity_cif = None

            with st.spinner("⏳ 正在提交任务..."):
                try:
                    uploaded_file.seek(0)
                    file_content = uploaded_file.getvalue().decode("utf-8")
                    task_id = predict_boltz2score(
                        file_content,
                        uploaded_file.name,
                        target_chain=target_chain_str or None,
                        ligand_chain=ligand_chain_str or None,
                    )
                    st.session_state.affinity_task_id = task_id
                    URLStateManager.update_url_for_affinity_task(task_id)
                    st.toast("任务已成功提交！", icon="🎉")
                    st.rerun()
                except requests.exceptions.RequestException as e:
                    st.error(f"⚠️ 提交失败：无法连接服务器。详情: {e}")
                    st.session_state.affinity_error = {"error_message": str(e), "type": "API Connection Error"}
                except Exception as e:
                    st.error(f"❌ 提交失败：{e}")
                    st.session_state.affinity_error = {"error_message": str(e), "type": "Client Error"}

    if st.session_state.affinity_task_id and st.session_state.affinity_results is None:
        st.divider()
        st.header("⏳ 任务进行中", anchor=False)

        if not st.session_state.affinity_error:
            status_placeholder = st.empty()
            while True:
                try:
                    status_data = get_status(st.session_state.affinity_task_id)
                    current_state = status_data.get('state', 'UNKNOWN')

                    with status_placeholder.container():
                        if current_state == 'SUCCESS':
                            st.success("🎉 预测完成，正在加载结果...")
                            try:
                                processed_results, _ = download_and_process_results(
                                    st.session_state.affinity_task_id
                                )
                                st.session_state.affinity_results = processed_results
                                st.session_state.affinity_cif = processed_results.get("cif")
                                st.toast("结果已成功加载！", icon="🎊")
                                st.rerun()
                                break
                            except Exception as e:
                                st.session_state.affinity_error = {
                                    "error_message": f"结果处理失败：{e}",
                                    "type": "Result File Error"
                                }
                                st.error(f"❌ 结果处理失败：{e}")
                                break
                        elif current_state == 'FAILURE':
                            st.session_state.affinity_error = status_data.get('info', {})
                            error_message = st.session_state.affinity_error.get('exc_message', '未知错误')
                            st.error(f"❌ 任务失败：{error_message}")
                            break
                        elif current_state in {'STARTED', 'PROGRESS'}:
                            task_info = status_data.get('info', {})
                            status_msg = task_info.get('status', '任务运行中...')
                            st.markdown('<div class="loader"></div>', unsafe_allow_html=True)
                            st.info(f"⏳ {status_msg} (每 10 秒自动刷新)")
                        elif current_state == 'PENDING':
                            st.markdown('<div class="loader"></div>', unsafe_allow_html=True)
                            st.info("⏳ 任务排队中，请稍候...")
                        else:
                            st.warning(f"❓ 当前状态: {current_state}")

                    time.sleep(10)
                except requests.exceptions.RequestException as e:
                    status_placeholder.error(f"🚨 无法获取任务状态：{e}")
                    st.session_state.affinity_error = {"error_message": str(e), "type": "API Connection Error"}
                    break
                except Exception as e:
                    status_placeholder.error(f"🚨 获取任务状态时发生错误：{e}")
                    st.session_state.affinity_error = {"error_message": str(e), "type": "Client Error"}
                    break

    if st.session_state.affinity_error:
        st.error("ℹ️ 任务执行失败，详细信息如下：")
        st.json(st.session_state.affinity_error)

        if st.button("🔄 重置并重新开始", key="reset_affinity", type="secondary", use_container_width=True):
            URLStateManager.clear_url_params()
            st.session_state.affinity_task_id = None
            st.session_state.affinity_results = None
            st.session_state.affinity_error = None
            st.session_state.affinity_cif = None
            st.rerun()

    if st.session_state.affinity_results is not None:
        st.divider()
        st.header("🎯 预测结果", anchor=False)

        results = st.session_state.affinity_results or {}
        cif_content = results.get("cif") or ""
        confidence_data = results.get("confidence") or {}
        affinity_data = results.get("affinity") or {}
        chain_map = results.get("chain_map") or {}

        col1, col2 = st.columns([2, 1])

        with col1:
            if cif_content:
                st.subheader("📊 3D 结构可视化", anchor=False)
                with st.expander("⚙️ 视图设置", expanded=True):
                    row1_col1, row1_col2 = st.columns(2)
                    with row1_col1:
                        st.selectbox("蛋白质样式", ['cartoon', 'stick', 'sphere'], key='affinity_protein_style_vis', index=0)
                    with row1_col2:
                        st.selectbox(
                            "着色方案",
                            ['pLDDT', 'Chain', 'Rainbow', 'Secondary Structure'],
                            key='affinity_color_scheme_vis',
                            index=0
                        )
                    row2_col1, row2_col2 = st.columns(2)
                    with row2_col1:
                        st.selectbox("配体样式", ['ball-and-stick', 'space-filling', 'stick', 'line'], key='affinity_ligand_style_vis', index=0)
                    with row2_col2:
                        st.checkbox("🔄 旋转模型", key='affinity_spin_model_vis', value=False)

                try:
                    structure = read_cif_from_string(cif_content)
                    protein_bfactors = extract_protein_residue_bfactors(structure)
                    view_html = visualize_structure_py3dmol(
                        cif_content=cif_content,
                        residue_bfactors=protein_bfactors,
                        protein_style=st.session_state.get('affinity_protein_style_vis', 'cartoon'),
                        ligand_style=st.session_state.get('affinity_ligand_style_vis', 'ball-and-stick'),
                        spin=st.session_state.get('affinity_spin_model_vis', False),
                        color_scheme=st.session_state.get('affinity_color_scheme_vis', 'pLDDT')
                    )
                    st.components.v1.html(view_html, height=600, scrolling=False)
                except Exception as e:
                    st.error(f"❌ 无法加载3D结构：{e}")

        with col2:
            st.markdown("**📈 结构置信度指标**")
            col_metrics = st.columns(2)
            col_metrics[0].metric(
                "平均 pLDDT",
                _format_metric_value(confidence_data.get('complex_plddt')),
            )
            col_metrics[1].metric(
                "pTM",
                _format_metric_value(confidence_data.get('ptm'), precision=4),
            )

            iptm_value = confidence_data.get('iptm')
            st.metric("ipTM", _format_metric_value(iptm_value, precision=4))

            pair_rows = _pair_iptm_rows(confidence_data, chain_map)
            if pair_rows:
                st.markdown("**所有链对的 pair ipTM**")
                st.dataframe(
                    pair_rows,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "chain_a": st.column_config.TextColumn("链 A"),
                        "chain_b": st.column_config.TextColumn("链 B"),
                        "pair_ipTM": st.column_config.NumberColumn("pair ipTM", format="%.4f"),
                    }
                )

            if affinity_data:
                st.markdown("**🧪 亲和力预测结果**")
                affinity_values = [
                    affinity_data.get(k)
                    for k in ['affinity_pred_value', 'affinity_pred_value1', 'affinity_pred_value2']
                    if isinstance(affinity_data.get(k), (int, float))
                ]
                if affinity_values:
                    log_ic50_in_uM = float(np.mean(affinity_values))
                    affinity_std = float(np.std(affinity_values)) if len(affinity_values) > 1 else 0.0
                    ic50 = math.pow(10, log_ic50_in_uM)
                    display_ic50 = f"{ic50:.2f} μM"
                    if affinity_std > 0:
                        ic50_std_lower = math.pow(10, log_ic50_in_uM - affinity_std)
                        ic50_std_upper = math.pow(10, log_ic50_in_uM + affinity_std)
                        display_ic50 = f"{ic50:.2f} μM ({ic50_std_lower:.2f}-{ic50_std_upper:.2f})"
                    st.metric("预测 IC50", display_ic50)

                binding_probs = [
                    affinity_data.get(k)
                    for k in ['affinity_probability_binary', 'affinity_probability_binary1', 'affinity_probability_binary2']
                    if isinstance(affinity_data.get(k), (int, float))
                ]
                if binding_probs:
                    st.metric("结合概率", f"{np.mean(binding_probs) * 100:.1f}%")

            if confidence_data or affinity_data:
                all_json_data = {"confidence": confidence_data, "affinity": affinity_data}
                st.download_button(
                    label="📥 下载预测指标 JSON",
                    data=json.dumps(all_json_data, indent=2, ensure_ascii=False),
                    file_name="boltz2score_metrics.json",
                    mime="application/json",
                    use_container_width=True,
                )
