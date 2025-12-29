
import streamlit as st
import requests
import pandas as pd
import io
import math
import numpy as np
import zipfile
import py3Dmol
import os

from frontend.prediction_client import predict_affinity, predict_affinity_separate, get_status
from frontend.utils import get_ligand_resnames_from_pdb, read_cif_from_string, extract_protein_residue_bfactors, visualize_structure_py3dmol
from frontend.url_state import URLStateManager

def render_affinity_page():
    # 尝试从URL恢复状态
    URLStateManager.restore_state_from_url()
    
    st.markdown("### 🔬 结合亲和力预测")
    st.markdown("预测蛋白质与小分子配体之间的结合强度，帮助您评估分子间的相互作用。")

    # Initialize affinity-specific session state variables with unique prefixes
    if 'affinity_task_id' not in st.session_state:
        st.session_state.affinity_task_id = None
    if 'affinity_results' not in st.session_state:
        st.session_state.affinity_results = None
    if 'affinity_error' not in st.session_state:
        st.session_state.affinity_error = None
    if 'affinity_ligand_resnames' not in st.session_state:
        st.session_state.affinity_ligand_resnames = []
    if 'affinity_cif' not in st.session_state:
        st.session_state.affinity_cif = None

    # Use affinity-specific variables to avoid conflicts with other tasks
    is_running = (st.session_state.affinity_task_id is not None and 
                 st.session_state.affinity_results is None and 
                 st.session_state.affinity_error is None)

    with st.expander("🏃‍♀️ **步骤 1: 上传结构文件**", expanded=not is_running and st.session_state.affinity_results is None):
        # Mode selection with better wording
        st.markdown("**选择您的文件类型：**")
        input_mode = st.radio(
            "文件类型",
            ["完整复合物", "蛋白质 + 小分子"],
            help="• **完整复合物**：包含蛋白质和配体的单个结构文件（PDB/CIF）\n• **蛋白质 + 小分子**：分别提供蛋白质结构文件和小分子结构文件",
            disabled=is_running,
            horizontal=True,
            label_visibility="collapsed"
        )
        
        st.divider()
        
        uploaded_file = None
        protein_file = None
        ligand_file = None
        
        if input_mode == "完整复合物":
            # Complex file mode with better layout
            st.markdown("**📋 上传完整的蛋白质-配体复合物文件**")
            uploaded_file = st.file_uploader(
                "选择结构文件", 
                type=['pdb', 'cif'], 
                disabled=is_running,
                help="支持 PDB 或 CIF 格式的复合物结构文件"
            )

            # Show detected ligands if file is uploaded
            if uploaded_file is not None and not is_running:
                file_content = uploaded_file.getvalue().decode("utf-8")
                if uploaded_file.name.lower().endswith('.pdb'):
                    # Import the validation function
                    from utils import validate_pdb_for_affinity
                    
                    validation_result = validate_pdb_for_affinity(file_content)
                    
                    if validation_result['valid']:
                        st.success(f"✅ 文件验证通过：检测到 {validation_result['atom_count']} 个蛋白质原子，{validation_result['hetatm_count']} 个配体原子")
                        st.success(f"✅ 检测到配体: {', '.join(validation_result['ligand_resnames'])}")
                        st.session_state.affinity_ligand_resnames = validation_result['ligand_resnames']
                    else:
                        st.error(f"❌ **文件验证失败**")
                        st.error(validation_result['error_message'])
                        
                        st.markdown("**💡 建议解决方案：**")
                        for i, suggestion in enumerate(validation_result['suggestions'], 1):
                            st.markdown(f"{i}. {suggestion}")
                        
                        if not validation_result['has_ligands'] and validation_result['has_protein']:
                            st.info("💡 **提示**：您可以使用下方的'蛋白质 + 小分子'模式，分别上传蛋白质文件和小分子文件。")
                        
                        st.session_state.affinity_ligand_resnames = []
                else:
                    detected_ligands = get_ligand_resnames_from_pdb(file_content)
                    if detected_ligands:
                        st.success(f"✅ 自动检测到配体: {', '.join(detected_ligands)}")
                        st.session_state.affinity_ligand_resnames = detected_ligands
                    else:
                        st.info("ℹ️ CIF文件的配体检测将在预测过程中进行")
                        st.session_state.affinity_ligand_resnames = []

            # Ligand residue name input
            col1, col2 = st.columns([2, 1])
            with col1:
                ligand_resname = st.text_input(
                    "配体名称", 
                    value="LIG" if not st.session_state.affinity_ligand_resnames else st.session_state.affinity_ligand_resnames[0], 
                    disabled=is_running, 
                    help="输入配体在结构文件中的三字母代码，如 LIG、UNK、ATP 等"
                )
            
            with col2:
                # Show dropdown for detected ligands
                if st.session_state.affinity_ligand_resnames and len(st.session_state.affinity_ligand_resnames) > 1:
                    selected_ligand = st.selectbox(
                        "或选择已检测到的配体:",
                        ["手动输入"] + st.session_state.affinity_ligand_resnames,
                        disabled=is_running,
                        help="从自动检测到的配体中选择"
                    )
                    if selected_ligand != "手动输入":
                        ligand_resname = selected_ligand

        else:  # 蛋白质 + 小分子模式
            st.markdown("**📋 分别上传蛋白质和小分子文件**")
            
            # Better vertical layout for separate files
            st.markdown("##### 🧬 蛋白质结构")
            protein_file = st.file_uploader(
                "上传蛋白质结构文件", 
                type=['pdb', 'cif'], 
                disabled=is_running,
                help="支持 PDB 或 CIF 格式的蛋白质结构文件"
            )
            
            st.markdown("##### 🧪 小分子配体")
            ligand_file = st.file_uploader(
                "上传小分子结构文件", 
                type=['sdf', 'mol', 'mol2'], 
                disabled=is_running,
                help="支持 SDF、MOL 或 MOL2 格式的小分子结构文件"
            )
            
            # Show file status
            if protein_file or ligand_file:
                if protein_file and ligand_file:
                    st.success(f"✅ 文件准备完成: {protein_file.name} + {ligand_file.name}")
                elif protein_file:
                    st.info(f"📁 已选择蛋白质文件: {protein_file.name} (请继续选择小分子文件)")
                elif ligand_file:
                    st.info(f"📁 已选择小分子文件: {ligand_file.name} (请继续选择蛋白质文件)")
            
            # Reset detected ligands for separate mode
            if protein_file and ligand_file:
                st.session_state.affinity_ligand_resnames = []
            
            # For separate mode, automatically use "LIG" as ligand name
            if input_mode == "蛋白质 + 小分子":
                ligand_resname = "LIG"  # Automatically set, no user input needed
                # st.info("💡 分开上传模式下，系统将自动生成标准PDB复合物文件，配体名称自动设为 'LIG'")
            else:
                # Ligand name for complex mode only
                ligand_resname = st.text_input(
                    "配体名称", 
                    value="LIG",
                    disabled=is_running, 
                    help="为小分子配体指定一个三字母名称，如 LIG、UNK 等"
                )

        # Submit button with better validation
        files_ready = False
        if input_mode == "完整复合物":
            files_ready = uploaded_file is not None and ligand_resname.strip()
        else:
            # For separate inputs, only need both files (ligand_resname is automatic)
            files_ready = protein_file is not None and ligand_file is not None

        # Show what's missing if not ready
        if not files_ready and not is_running:
            missing_items = []
            if input_mode == "完整复合物":
                if not uploaded_file:
                    missing_items.append("复合物结构文件")
                if not ligand_resname.strip():
                    missing_items.append("配体名称")
            else:
                # For separate inputs, only check files (ligand_resname is automatic)
                if not protein_file:
                    missing_items.append("蛋白质结构文件")
                if not ligand_file:
                    missing_items.append("小分子结构文件")
            
            if missing_items:
                st.warning(f"⚠️ 请完成以下步骤: {' • '.join(missing_items)}")

        if st.button("🚀 开始预测", key="start_affinity", type="primary", disabled=is_running or not files_ready, use_container_width=True):
            st.session_state.affinity_task_id = None
            st.session_state.affinity_results = None
            st.session_state.affinity_error = None
            st.session_state.affinity_cif = None

            with st.spinner("⏳ 正在提交预测任务，请稍候..."):
                try:
                    if input_mode == "完整复合物":
                        # Complex file mode
                        uploaded_file.seek(0)
                        file_content = uploaded_file.getvalue().decode("utf-8")
                        file_name = uploaded_file.name
                        task_id = predict_affinity(file_content, file_name, ligand_resname.strip())
                        # 更新URL参数以保持亲和力任务状态
                        URLStateManager.update_url_for_affinity_task(task_id)
                    else:
                        # Separate files mode - remove output_prefix parameter
                        protein_file.seek(0)
                        ligand_file.seek(0)
                        task_id = predict_affinity_separate(
                            protein_file.getvalue().decode("utf-8"),
                            protein_file.name,
                            ligand_file.getvalue(),
                            ligand_file.name,
                            ligand_resname.strip(),
                            "complex"  # Fixed prefix, no user input needed
                        )
                    
                    st.session_state.affinity_task_id = task_id
                    
                    # 更新URL参数以保持亲和力任务状态
                    URLStateManager.update_url_for_affinity_task(task_id)
                    
                    st.toast("任务已成功提交！", icon="🎉")
                    st.rerun()
                except requests.exceptions.RequestException as e:
                    st.error(f"⚠️ **提交失败：无法连接到服务器**\n\n请检查后端服务是否正常运行。\n\n详细错误: {e}")
                    st.session_state.affinity_error = {"error_message": str(e), "type": "API Connection Error"}
                except Exception as e:
                    st.error(f"❌ **提交失败：发生未知错误**\n\n详细错误: {e}")
                    st.session_state.affinity_error = {"error_message": str(e), "type": "Client Error"}

    if st.session_state.affinity_task_id and st.session_state.affinity_results is None:
        st.divider()
        st.header("⏳ **预测进行中**", anchor=False)
        
        if not st.session_state.affinity_error:
            spinner_and_status_placeholder = st.empty()

            while True:
                try:
                    status_data = get_status(st.session_state.affinity_task_id)
                    current_state = status_data.get('state', 'UNKNOWN')

                    with spinner_and_status_placeholder.container():
                        if current_state == 'SUCCESS':
                            st.success("🎉 预测完成！正在处理结果...")
                            try:
                                results_url = f"http://localhost:5000/results/{st.session_state.affinity_task_id}"
                                response = requests.get(results_url)
                                response.raise_for_status()
                                
                                with zipfile.ZipFile(io.BytesIO(response.content)) as z:
                                    cif_content = None
                                    pdb_content = None
                                    
                                    for filename in z.namelist():
                                        if filename.endswith('.csv'):
                                            with z.open(filename) as f:
                                                df = pd.read_csv(f)
                                                st.session_state.affinity_results = df
                                        elif filename.endswith('.cif'):
                                            with z.open(filename) as f:
                                                cif_content = f.read().decode("utf-8")
                                        elif filename.endswith('.pdb'):
                                            with z.open(filename) as f:
                                                pdb_content = f.read().decode("utf-8")
                                    
                                    # Prefer CIF content, fallback to PDB with conversion
                                    if cif_content:
                                        st.session_state.affinity_cif = cif_content
                                    elif pdb_content:
                                        # Convert PDB to CIF for visualization
                                        try:
                                            import tempfile
                                            import subprocess
                                            
                                            with tempfile.NamedTemporaryFile(mode='w', suffix='.pdb', delete=False) as pdb_file:
                                                pdb_file.write(pdb_content)
                                                pdb_file_path = pdb_file.name
                                            
                                            cif_file_path = pdb_file_path.replace('.pdb', '.cif')
                                            result = subprocess.run(['maxit', '-input', pdb_file_path, '-output', cif_file_path, '-o', '1'], 
                                                                  check=True, capture_output=True, text=True)
                                            
                                            if os.path.exists(cif_file_path):
                                                with open(cif_file_path, 'r') as f:
                                                    converted_cif = f.read()
                                                st.session_state.affinity_cif = converted_cif
                                                
                                            # Clean up temp files
                                            os.unlink(pdb_file_path)
                                            if os.path.exists(cif_file_path):
                                                os.unlink(cif_file_path)
                                                
                                        except Exception as conv_error:
                                            # If conversion fails, store PDB content for debugging
                                            st.session_state.affinity_cif = pdb_content
                                            print(f"Warning: Could not convert PDB to CIF: {conv_error}")

                                st.toast("结果已成功加载！", icon="🎊")
                                st.rerun()
                                break
                            except Exception as e:
                                st.session_state.affinity_error = {"error_message": f"结果文件处理失败：{e}", "type": "Result File Error"}
                                st.error(f"❌ **结果处理失败**\n\n{e}")
                                break
                        elif current_state == 'FAILURE':
                            st.session_state.affinity_error = status_data.get('info', {})
                            error_message = st.session_state.affinity_error.get('exc_message', '未知错误')
                            
                            # Provide user-friendly error messages
                            if "No ligand molecules (HETATM records) found" in error_message:
                                user_friendly_message = """
                                ❌ **PDB文件中未找到配体分子**
                                
                                您上传的PDB文件只包含蛋白质原子，没有配体分子（HETATM记录）。
                                亲和力预测需要蛋白质-配体复合物结构。
                                
                                **解决方案：**
                                1. **使用完整复合物文件**：上传包含蛋白质和配体的PDB文件
                                2. **使用分离输入模式**：分别上传蛋白质PDB文件和配体SDF文件
                                3. **添加配体坐标**：在PDB文件中添加配体分子的HETATM记录
                                
                                💡 **建议**：如果您只有蛋白质结构，请使用"蛋白质 + 小分子"输入模式。
                                """
                            elif "No HETATM records found" in error_message:
                                user_friendly_message = """
                                ❌ **未找到配体分子**
                                
                                在上传的文件中未找到指定名称的配体。可能的原因：
                                • 文件中不包含小分子配体
                                • 配体名称输入错误
                                • 文件格式问题
                                
                                **解决建议：**
                                • 确认文件包含小分子配体
                                • 检查配体名称是否正确
                                • 尝试使用"蛋白质 + 小分子"模式
                                """
                            elif "Ligand residue name" in error_message and "not found" in error_message:
                                user_friendly_message = """
                                ❌ **配体名称不匹配**
                                
                                在文件中未找到您指定的配体名称。
                                
                                **解决建议：**
                                • 检查错误详情中列出的可用配体名称
                                • 重新选择正确的配体名称
                                • 或尝试使用自动检测到的配体名称
                                """
                            elif "No ligand molecules found" in error_message:
                                user_friendly_message = """
                                ❌ **文件中无配体分子**
                                
                                上传的文件中未检测到配体分子。
                                
                                **解决建议：**
                                • 确保文件包含蛋白质-配体复合物
                                • 尝试使用"蛋白质 + 小分子"模式分别上传文件
                                """
                            elif "Failed to parse ligand" in error_message:
                                user_friendly_message = """
                                ❌ **配体结构解析失败**
                                
                                无法正确解析配体的结构信息。
                                
                                **解决建议：**
                                • 检查文件格式是否正确
                                • 确认配体结构的完整性
                                • 尝试使用其他格式的文件
                                """
                            else:
                                user_friendly_message = f"""
                                ❌ **预测失败**
                                
                                {error_message}
                                """
                            
                            st.error(user_friendly_message)
                            
                            # Show detailed error in expander
                            with st.expander("🔍 查看技术详情"):
                                st.code(error_message)
                            break
                        elif current_state == 'PENDING':
                            st.markdown('<div class="loader"></div>', unsafe_allow_html=True)
                            st.info("� 任务正在排队中，请耐心等待...")
                        elif current_state == 'STARTED' or current_state == 'PROGRESS':
                            task_info = status_data.get('info', {})
                            if isinstance(task_info, dict) and 'status' in task_info:
                                # Filter out GPU information
                                status_msg = task_info['status']
                                if "Running affinity prediction on GPU" in status_msg:
                                    status_msg = "正在分析分子间相互作用..."
                                st.markdown('<div class="loader"></div>', unsafe_allow_html=True)
                                st.info(f"**任务正在运行**：{status_msg} (页面将每 10 秒自动刷新)", icon="⏳")
                            else:
                                st.markdown('<div class="loader"></div>', unsafe_allow_html=True)
                                st.info("**任务正在运行**：正在分析分子间相互作用... (页面将每 10 秒自动刷新)", icon="⏳")
                        else:
                            st.markdown('<div class="loader"></div>', unsafe_allow_html=True)
                            st.warning(f"❓ 任务状态未知或正在初始化... (当前状态: **{current_state}**)")

                    import time
                    time.sleep(10)  # Keep consistent with prediction page
                except requests.exceptions.RequestException as e:
                    spinner_and_status_placeholder.error(f"🚨 **无法获取任务状态：API连接失败**。请检查后端服务是否运行正常。详情: {e}")
                    st.session_state.affinity_error = {"error_message": str(e), "type": "API Connection Error"}
                    break
                except Exception as e:
                    spinner_and_status_placeholder.error(f"🚨 **获取任务状态时发生未知错误**。详情: {e}")
                    st.session_state.affinity_error = {"error_message": str(e), "type": "Client Error"}
                    break

    # Error handling section
    if st.session_state.affinity_error:
        st.error("ℹ️ 任务执行失败，详细信息如下：")
        st.json(st.session_state.affinity_error)
        
        if st.button("🔄 重置并重新开始", key="reset_affinity", type="secondary", use_container_width=True):
            # 清除URL参数
            URLStateManager.clear_url_params()
            st.session_state.affinity_task_id = None
            st.session_state.affinity_results = None
            st.session_state.affinity_error = None
            st.session_state.affinity_ligand_resnames = []
            st.session_state.affinity_cif = None
            st.rerun()

    if st.session_state.affinity_results is not None:
        st.divider()
        st.header("🎯 **预测结果**", anchor=False)
        
        col1, col2 = st.columns([2,1])

        with col1:
            if st.session_state.affinity_cif:
                st.subheader("📊 3D 结构可视化", anchor=False)
                with st.expander("⚙️ **视图设置**", expanded=True):
                    row1_col1, row1_col2 = st.columns(2)
                    with row1_col1:
                        st.selectbox("蛋白质样式", ['cartoon', 'stick', 'sphere'], key='affinity_protein_style_vis', help="选择蛋白质的渲染样式", index=0)
                    with row1_col2:
                        st.selectbox(
                            "着色方案",
                            ['pLDDT', 'Chain', 'Rainbow', 'Secondary Structure'],
                            key='affinity_color_scheme_vis',
                            help="选择分子的着色方式：pLDDT（置信度）、Chain（链）、Rainbow（彩虹）、二级结构",
                            index=0
                        )
                    row2_col1, row2_col2 = st.columns(2)
                    with row2_col1:
                        st.selectbox("配体样式", ['ball-and-stick', 'space-filling', 'stick', 'line'], key='affinity_ligand_style_vis', help="选择小分子配体的渲染样式", index=0)
                    with row2_col2:
                        st.checkbox("🔄 旋转模型", key='affinity_spin_model_vis', value=False, help="勾选后模型将自动旋转")
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
                    error_msg = str(e)
                    st.error(f"❌ 无法加载3D结构：{error_msg}")
                    
                    # Debug information to help identify the issue
                    with st.expander("🐛 调试信息", expanded=False):
                        cif_content = st.session_state.affinity_cif
                        st.write("**CIF内容统计:**")
                        st.write(f"- 总长度: {len(cif_content)} 字符")
                        st.write(f"- 是否以 'data_' 开头: {cif_content.strip().startswith('data_')}")
                        st.write(f"- 包含 '_atom_site' 标签: {'_atom_site' in cif_content}")
                        
                        # Show first few lines
                        lines = cif_content.split('\n')[:10]
                        st.write("**前10行内容:**")
                        st.code('\n'.join(lines), language="text")
                        
                        # Test individual components
                        st.write("**组件测试:**")
                        try:
                            import py3Dmol
                            view = py3Dmol.view(width='100%', height=600)
                            view.addModel(cif_content, 'cif')
                            st.success("✓ py3Dmol 可以解析 CIF 内容")
                        except Exception as py3d_error:
                            st.error(f"✗ py3Dmol 解析失败: {py3d_error}")
                        
                        try:
                            structure_test = read_cif_from_string(cif_content)
                            st.success("✓ BioPython 可以解析 CIF 内容")
                        except Exception as bio_error:
                            st.error(f"✗ BioPython 解析失败: {bio_error}")

        with col2:
            results_df = st.session_state.affinity_results
            if not results_df.empty:
                affinity_data = results_df.iloc[0].to_dict()
                
                st.markdown("**📈 亲和力预测结果**")
                
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
                        st.metric("预测 IC50", display_ic50_with_std, help=f"半数抑制浓度，基于 {len(affinity_values)} 个预测值。数值越低表示亲和力越强")
                    else:
                        display_ic50 = f"{ic50_uM:.3f} µM"
                        st.metric("预测 IC50", display_ic50, help="半数抑制浓度，数值越低表示亲和力越强")

                binding_probabilities = []
                for key in ['affinity_probability_binary', 'affinity_probability_binary1', 'affinity_probability_binary2']:
                    if key in affinity_data and pd.notna(affinity_data[key]):
                        binding_probabilities.append(affinity_data[key])
                
                if binding_probabilities:
                    binder_prob = np.mean(binding_probabilities)
                    binding_prob_std = np.std(binding_probabilities) if len(binding_probabilities) > 1 else 0.0
                    
                    if len(binding_probabilities) > 1:
                        st.metric("结合概率", f"{binder_prob:.2%} ± {binding_prob_std:.2%}", help=f"预测形成稳定复合物的概率，基于 {len(binding_probabilities)} 个预测值")
                    else:
                        st.metric("结合概率", f"{binder_prob:.2%}", help="预测形成稳定复合物的概率")
