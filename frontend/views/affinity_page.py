
import streamlit as st
import requests
import pandas as pd
import io
import math
import numpy as np
import zipfile
import py3Dmol

from frontend.prediction_client import predict_affinity, get_status
from frontend.utils import get_ligand_resnames_from_pdb, read_cif_from_string, extract_protein_residue_bfactors, visualize_structure_py3dmol

def render_affinity_page():
    st.markdown("### 🔬 结合亲和力预测")
    st.markdown("上传您的蛋白质-配体复合物结构文件（PDB或CIF格式），预测它们之间的结合亲和力。")

    # Initialize session state variables
    if 'affinity_task_id' not in st.session_state:
        st.session_state.affinity_task_id = None
    if 'affinity_results' not in st.session_state:
        st.session_state.affinity_results = None
    if 'affinity_error' not in st.session_state:
        st.session_state.affinity_error = None
    if 'ligand_resnames' not in st.session_state:
        st.session_state.ligand_resnames = []
    if 'affinity_cif' not in st.session_state:
        st.session_state.affinity_cif = None

    is_running = st.session_state.affinity_task_id is not None and st.session_state.affinity_results is None and st.session_state.affinity_error is None

    with st.expander("🔧 **步骤 1: 上传您的结构文件**", expanded=not is_running and st.session_state.affinity_results is None):
        uploaded_file = st.file_uploader("上传 PDB 或 CIF 文件", type=['pdb', 'cif'], disabled=is_running)

        # Show detected ligands if file is uploaded
        if uploaded_file is not None and not is_running:
            file_content = uploaded_file.getvalue().decode("utf-8")
            if uploaded_file.name.lower().endswith('.pdb'):
                detected_ligands = get_ligand_resnames_from_pdb(file_content)
                if detected_ligands:
                    st.success(f"✅ 检测到配体残基名: {', '.join(detected_ligands)}")
                    st.session_state.ligand_resnames = detected_ligands
                else:
                    st.warning("⚠️ **警告**: 在PDB文件中未检测到HETATM记录。请确认文件包含配体分子。")
                    st.session_state.ligand_resnames = []
            else:
                st.info("ℹ️ 对于CIF文件，配体检测将在预测过程中进行。")

        ligand_resname = st.text_input(
            "配体残基名", 
            value="LIG" if not st.session_state.ligand_resnames else st.session_state.ligand_resnames[0], 
            disabled=is_running, 
            help="请输入您在PDB文件中为配体指定的三个字母的残基名，例如 LIG, UNK, DRG 等。"
        )
        
        # Show dropdown for detected ligands
        if st.session_state.ligand_resnames and len(st.session_state.ligand_resnames) > 1:
            selected_ligand = st.selectbox(
                "或选择检测到的配体残基名:",
                [""] + st.session_state.ligand_resnames,
                disabled=is_running,
                help="从检测到的配体残基名中选择一个"
            )
            if selected_ligand:
                ligand_resname = selected_ligand

        if st.button("🚀 开始亲和力预测", type="primary", disabled=is_running or not uploaded_file or not ligand_resname, use_container_width=True):
            st.session_state.affinity_task_id = None
            st.session_state.affinity_results = None
            st.session_state.affinity_error = None
            st.session_state.affinity_cif = None

            if uploaded_file is not None:
                uploaded_file.seek(0)
                file_content = uploaded_file.getvalue().decode("utf-8")
                file_name = uploaded_file.name

                with st.spinner("⏳ 正在提交任务，请稍候..."):
                    try:
                        task_id = predict_affinity(file_content, file_name, ligand_resname)
                        st.session_state.affinity_task_id = task_id
                        st.toast("🎉 任务已提交！", icon="✅")
                        st.rerun()
                    except requests.exceptions.RequestException as e:
                        st.error(f"⚠️ **任务提交失败：无法连接到API服务器或服务器返回错误**。请检查后端服务是否运行正常。详情: {e}")
                        st.session_state.affinity_error = {"error_message": str(e), "type": "API Connection Error"}
                    except Exception as e:
                        st.error(f"❌ **任务提交失败：发生未知错误**。详情: {e}")
                        st.session_state.affinity_error = {"error_message": str(e), "type": "Client Error"}

    if st.session_state.affinity_task_id and st.session_state.affinity_results is None:
        st.divider()
        st.header("✨ **步骤 2: 查看预测结果**", anchor=False)
        if not st.session_state.affinity_error:
            spinner_and_status_placeholder = st.empty()

            while True:
                try:
                    status_data = get_status(st.session_state.affinity_task_id)
                    current_state = status_data.get('state', 'UNKNOWN')

                    with spinner_and_status_placeholder.container():
                        if current_state == 'SUCCESS':
                            st.success("🎉 任务成功完成！正在下载并显示结果...")
                            try:
                                results_url = f"http://localhost:5000/results/{st.session_state.affinity_task_id}"
                                response = requests.get(results_url)
                                response.raise_for_status()
                                
                                with zipfile.ZipFile(io.BytesIO(response.content)) as z:
                                    for filename in z.namelist():
                                        if filename.endswith('.csv'):
                                            with z.open(filename) as f:
                                                df = pd.read_csv(f)
                                                st.session_state.affinity_results = df
                                        elif filename.endswith('.pdb') or filename.endswith('.cif'):
                                            with z.open(filename) as f:
                                                st.session_state.affinity_cif = f.read().decode("utf-8")

                                st.toast("✅ 结果已成功加载！", icon="🎊")
                                st.rerun()
                                break
                            except Exception as e:
                                st.session_state.affinity_error = {"error_message": f"处理结果文件失败：{e}", "type": "Result File Error"}
                                st.error(f"❌ **结果文件处理失败**：{e}")
                                break
                        elif current_state == 'FAILURE':
                            st.session_state.affinity_error = status_data.get('info', {})
                            error_message = st.session_state.affinity_error.get('exc_message', '未知错误')
                            
                            # Provide more user-friendly error messages
                            if "No HETATM records found" in error_message:
                                user_friendly_message = ("❌ **配体未找到**: 在您上传的文件中未找到指定的配体残基名。"
                                                        "请检查文件是否包含配体分子，或确认配体残基名是否正确。")
                            elif "Ligand residue name" in error_message and "not found" in error_message:
                                user_friendly_message = ("❌ **配体残基名错误**: 在文件中未找到您指定的配体残基名。"
                                                        "请检查错误信息中列出的可用残基名，然后重新选择正确的配体残基名。")
                            elif "No ligand molecules found" in error_message:
                                user_friendly_message = ("❌ **无配体分子**: 上传的文件中未检测到配体分子。"
                                                        "亲和力预测需要蛋白质-配体复合物结构。请上传包含配体的结构文件。")
                            elif "Failed to parse ligand" in error_message:
                                user_friendly_message = ("❌ **配体解析失败**: 无法解析指定的配体结构。"
                                                        "配体的原子坐标可能有问题，请检查文件格式和配体结构的完整性。")
                            else:
                                user_friendly_message = f"❌ **任务失败**: {error_message}"
                            
                            st.error(user_friendly_message)
                            
                            # Show detailed error in expander
                            with st.expander("🔍 查看详细错误信息"):
                                st.text(error_message)
                            break
                        else:
                            st.markdown('<div class="loader"></div>', unsafe_allow_html=True)
                            st.info(f"🔬 **任务正在运行**：当前状态: {current_state} (页面将每 10 秒自动刷新)", icon="⏳")

                    import time
                    time.sleep(10)
                except requests.exceptions.RequestException as e:
                    spinner_and_status_placeholder.error(f"🚨 **无法获取任务状态：API连接失败**。请检查后端服务是否运行正常。详情: {e}")
                    st.session_state.affinity_error = {"error_message": str(e), "type": "API Connection Error"}
                    break
                except Exception as e:
                    spinner_and_status_placeholder.error(f"🚨 **获取任务状态时发生未知错误**。详情: {e}")
                    st.session_state.affinity_error = {"error_message": str(e), "type": "Client Error"}
                    break

    if st.session_state.affinity_error:
        st.error("ℹ️ 任务执行失败，详细信息如下：")
        st.json(st.session_state.affinity_error)
        if st.button("🔄 重置并重新开始", type="secondary"):
            st.session_state.affinity_task_id = None
            st.session_state.affinity_results = None
            st.session_state.affinity_error = None
            st.session_state.ligand_resnames = []
            st.session_state.affinity_cif = None
            st.rerun()

    if st.session_state.affinity_results is not None:
        st.divider()
        st.header("✅ **步骤 2: 预测结果**", anchor=False)
        
        col1, col2 = st.columns([2,1])

        with col1:
            if st.session_state.affinity_cif:
                st.subheader("📊 3D 结构可视化", anchor=False)
                with st.expander("⚙️ **视图设置**", expanded=True):
                    row1_col1, row1_col2 = st.columns(2)
                    with row1_col1:
                        st.selectbox("大分子样式", ['cartoon', 'stick', 'sphere'], key='affinity_protein_style_vis', help="选择蛋白质、DNA、RNA 等大分子的渲染样式。", index=0)
                    with row1_col2:
                        st.selectbox(
                            "着色方案",
                            ['pLDDT', 'Chain', 'Rainbow', 'Secondary Structure'],
                            key='affinity_color_scheme_vis',
                            help="""
                            选择整个复合物的着色方式：
                            - **pLDDT**: 根据预测置信度着色 (默认)，蓝色表示高置信，橙色表示低置信。
                            - **Chain**: 按不同的分子链着色。
                            - **Rainbow**: 从N端到C端按彩虹色渐变。
                            - **Secondary Structure**: 根据分子的二级结构（如螺旋、折叠）着色。
                            """,
                            index=0
                        )
                    row2_col1, row2_col2 = st.columns(2)
                    with row2_col1:
                        st.selectbox("配体样式", ['ball-and-stick', 'space-filling', 'stick', 'line'], key='affinity_ligand_style_vis', help="选择小分子的渲染样式。", index=0)
                    with row2_col2:
                        st.checkbox("🔄 旋转模型", key='affinity_spin_model_vis', value=False, help="勾选后，模型将自动围绕Z轴旋转。" )
                try:
                    structure = read_cif_from_string(st.session_state.affinity_cif)
                    protein_bfactors = extract_protein_residue_bfactors(structure)
                    view_html = visualize_structure_py3dmol(
                        cif_content=st.session_state.affinity_cif, 
                        residue_bfactors=protein_bfactors,
                        protein_style=st.session_state.get('affinity_protein_style_vis', 'cartoon'),
                        ligand_style=st.session_state.get('affinity_ligand_style_vis', 'ball-and-stick'),
                        spin=st.session_state.get('affinity_spin_model_vis', False),
                        color_scheme=st.session_state.get('affinity_color_scheme_vis', 'pLDDT')
                    )
                    st.components.v1.html(view_html, height=600, scrolling=False)
                except Exception as e:
                    st.error(f"无法加载3D结构：{e}")

        with col2:
            results_df = st.session_state.affinity_results
            if not results_df.empty:
                affinity_data = results_df.iloc[0].to_dict()
                
                st.markdown("<b>亲和力预测指标</b>", unsafe_allow_html=True)
                
                affinity_values = []
                for key in ['affinity_pred_value', 'affinity_pred_value1', 'affinity_pred_value2']:
                    if key in affinity_data and pd.notna(affinity_data[key]):
                        affinity_values.append(affinity_data[key])
                
                if affinity_values:
                    log_ic50_in_uM = np.mean(affinity_values)
                    affinity_std = np.std(affinity_values) if len(affinity_values) > 1 else 0.0
                    
                    ic50_uM = math.pow(10, log_ic50_in_uM)
                    
                    if len(affinity_values) > 1:
                        ic50_std_lower = math.pow(10, log_ic50_in_uM - affinity_std)
                        ic50_std_upper = math.pow(10, log_ic50_in_uM + affinity_std)
                        display_ic50_with_std = f"{ic50_uM:.3f} ± {(ic50_std_upper-ic50_std_lower)/2:.3f} μM"
                        st.metric("预测 IC50", display_ic50_with_std, help=f"预测的半数抑制浓度 (IC50)，基于 {len(affinity_values)} 个预测值的平均结果。数值越低表示预测的亲和力越强。" )
                    else:
                        display_ic50 = f"{ic50_uM:.3f} µM"
                        st.metric("预测 IC50", display_ic50, help="预测的半数抑制浓度 (IC50)。数值越低表示预测的亲和力越强。" )

                binding_probabilities = []
                for key in ['affinity_probability_binary', 'affinity_probability_binary1', 'affinity_probability_binary2']:
                    if key in affinity_data and pd.notna(affinity_data[key]):
                        binding_probabilities.append(affinity_data[key])
                
                if binding_probabilities:
                    binder_prob = np.mean(binding_probabilities)
                    binding_prob_std = np.std(binding_probabilities) if len(binding_probabilities) > 1 else 0.0
                    
                    if len(binding_probabilities) > 1:
                        st.metric("结合概率", f"{binder_prob:.2%} ± {binding_prob_std:.2%}", help=f"模型预测结合体与其余组分形成稳定复合物的概率，基于 {len(binding_probabilities)} 个预测值的平均结果。" )
                    else:
                        st.metric("结合概率", f"{binder_prob:.2%}", help="模型预测结合体与其余组分形成稳定复合物的概率。" )
