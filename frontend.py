import streamlit as st
import requests
import time
import json
import zipfile
import io
import yaml
import string
import uuid
import py3Dmol
from Bio.PDB import MMCIFParser, PDBIO
from Bio.PDB.Structure import Structure
import math
import os
from streamlit_ketcher import st_ketcher

API_URL = "http://127.0.0.1:5000"
TYPE_TO_DISPLAY = {
    'protein': '🧬 蛋白质',
    'ligand': '💊 小分子',
    'dna': '🔗 DNA',
    'rna': '📜 RNA'
}

TYPE_SPECIFIC_INFO = {
    'protein': {
        'placeholder': "例如: MVSKGEELFTGVVPILVELDGDVNGHKFSVSGEGEGDATYGKLTLKFICTTGKLPVPWPTLV...",
        'help': "请输入标准的单字母氨基酸序列。"
    },
    'dna': {
        'placeholder': "例如: GTCGAC... (A, T, C, G)",
        'help': "请输入标准的单字母脱氧核糖核酸序列 (A, T, C, G)。"
    },
    'rna': {
        'placeholder': "例如: GUCGAC... (A, U, C, G)",
        'help': "请输入标准的单字母核糖核酸序列 (A, U, C, G)。"
    }
}


def read_cif_from_string(cif_content: str) -> Structure:
    """Parses a CIF string into a BioPython Structure object."""
    parser = MMCIFParser(QUIET=True)
    file_like = io.StringIO(cif_content)
    structure = parser.get_structure('protein', file_like)
    return structure

def extract_protein_residue_bfactors(structure: Structure):
    """Extracts b-factors for protein/rna/dna residues only."""
    residue_bfactors = {}
    for model in structure:
        for chain in model:
            for residue in chain:
                hetfield = residue.get_id()[0]
                if hetfield.strip() == "":
                    resseq = residue.get_id()[1]
                    chain_id = chain.id
                    atom_bfactors = [atom.get_bfactor() for atom in residue]
                    if atom_bfactors:
                        avg_bfactor = sum(atom_bfactors) / len(atom_bfactors)
                        residue_bfactors[(chain_id, resseq)] = avg_bfactor
    return residue_bfactors

def get_color_from_bfactor(bfactor: float) -> str:
    """Maps a b-factor (pLDDT score) to a specific color."""
    if bfactor >= 90: return '#0053D6'
    elif 70 <= bfactor < 90: return '#65CBF3'
    elif 50 <= bfactor < 70: return '#FFDB13'
    else: return '#FF7D45'

def visualize_structure_py3dmol(
    cif_content: str,
    residue_bfactors: dict,
    protein_style: str = 'cartoon',
    ligand_style: str = 'ball-and-stick',
    spin: bool = False,
    color_scheme: str = 'pLDDT'
) -> str:
    view = py3Dmol.view(width='100%', height=600)
    view.addModel(cif_content, 'cif')

    if color_scheme == 'pLDDT':
        if protein_style == 'cartoon':
            view.setStyle({'model': -1, 'hetflag': False}, {'cartoon': {'color': 'white'}})
            for (chain_id, resseq), avg_bfactor in residue_bfactors.items():
                color = get_color_from_bfactor(avg_bfactor)
                view.setStyle({'chain': chain_id, 'resi': resseq}, {'cartoon': {'color': color}})
        elif protein_style == 'stick':
            view.setStyle({'model': -1, 'hetflag': False}, {'stick': {'colorscheme': 'default'}})
        elif protein_style == 'sphere':
            view.setStyle({'model': -1, 'hetflag': False}, {'sphere': {'colorscheme': 'default'}})

        ligand_color_map = {}
        try:
            parsed_structure = read_cif_from_string(cif_content)
            for atom in parsed_structure.get_atoms():
                residue = atom.get_parent()
                if residue.get_id()[0].strip() != "":
                    serial = atom.get_serial_number()
                    bfactor = atom.get_bfactor()
                    color = get_color_from_bfactor(bfactor)
                    ligand_color_map[serial-1] = color
        except Exception as e:
            print(f"Error parsing CIF for ligand coloring: {e}")
        
        custom_colorscheme = {'prop': 'serial', 'map': ligand_color_map}
        if ligand_color_map:
            if ligand_style == 'ball-and-stick':
                view.setStyle({'hetflag': True}, {'stick': {'colorscheme': custom_colorscheme, 'radius': 0.15}})
                view.addStyle({'hetflag': True}, {'sphere': {'colorscheme': custom_colorscheme, 'scale': 0.25}})
            elif ligand_style == 'space-filling':
                view.setStyle({'hetflag': True}, {'sphere': {'colorscheme': custom_colorscheme}})
            elif ligand_style == 'stick':
                view.setStyle({'hetflag': True}, {'stick': {'colorscheme': custom_colorscheme, 'radius': 0.25}})
            elif ligand_style == 'line':
                view.setStyle({'hetflag': True}, {'line': {'colorscheme': custom_colorscheme}})
        else:
            view.setStyle({'hetflag': True}, {'stick': {'colorscheme': 'default'}})

    else:
        common_scheme_name = 'chain'
        if color_scheme == 'Rainbow':
            common_scheme_name = 'spectrum'
        elif color_scheme == 'Secondary Structure':
            if protein_style == 'cartoon':
                common_scheme_name = 'ssPyMOL'
            else:
                common_scheme_name = 'chain'

        view.setStyle({'hetflag': False}, {protein_style: {'colorscheme': common_scheme_name}})

        if ligand_style == 'ball-and-stick':
            view.setStyle({'hetflag': True}, {'stick': {'colorscheme': common_scheme_name, 'radius': 0.15}})
            view.addStyle({'hetflag': True}, {'sphere': {'colorscheme': common_scheme_name, 'scale': 0.25}})
        elif ligand_style == 'space-filling':
            view.setStyle({'hetflag': True}, {'sphere': {'colorscheme': common_scheme_name}})
        elif ligand_style == 'stick':
            view.setStyle({'hetflag': True}, {'stick': {'colorscheme': common_scheme_name, 'radius': 0.25}})
        elif ligand_style == 'line':
            view.setStyle({'hetflag': True}, {'line': {'colorscheme': common_scheme_name}})

    view.setBackgroundColor('#F0F2F6')
    view.zoomTo()
    if spin:
        view.spin(True)

    return view._make_html()

def export_to_pdb(cif_content: str) -> str:
    """Converts CIF content to PDB string."""
    structure = read_cif_from_string(cif_content)
    pdb_io = PDBIO()
    pdb_io.set_structure(structure)
    pdb_buffer = io.StringIO()
    pdb_io.save(pdb_buffer)
    return pdb_buffer.getvalue()

def submit_job(yaml_content: str, use_msa: bool) -> str:
    """
    提交预测任务到后端 API。
    """
    files = {'yaml_file': ('input.yaml', yaml_content)}
    data = {'use_msa_server': str(use_msa).lower(), 'priority': 'high'}
    headers = {'X-API-Token': os.getenv('API_SECRET_TOKEN', 'your_default_api_token')}
    
    response = requests.post(f"{API_URL}/predict", files=files, data=data, headers=headers)
    response.raise_for_status()
    return response.json()['task_id']

def get_status(task_id: str) -> dict:
    """
    查询指定 task_id 的 Celery 任务状态。
    """
    response = requests.get(f"{API_URL}/status/{task_id}")
    response.raise_for_status()
    return response.json()

def download_and_process_results(task_id: str) -> tuple[dict, bytes]:
    """
    下载并处理任务结果ZIP文件。
    """
    response = requests.get(f"{API_URL}/results/{task_id}", stream=True)
    response.raise_for_status()
    raw_zip_bytes = response.content
    zip_buffer = io.BytesIO(raw_zip_bytes)
    results = {}
    cif_candidate, confidence_candidate, affinity_candidate = None, None, None
    with zipfile.ZipFile(zip_buffer, 'r') as zf:
        all_files = zf.namelist()
        for filename in all_files:
            if filename.endswith((".cif", ".pdb")):
                if "_model_0.cif" in filename or "ranked_0.cif" in filename:
                    cif_candidate = filename
                elif "_unrelaxed_rank_001_alphafold2_ptm.pdb" in filename:
                    cif_candidate = filename
                elif cif_candidate is None:
                    cif_candidate = filename
            if "confidence" in filename and filename.endswith(".json"):
                confidence_candidate = filename
            if "affinity" in filename and filename.endswith(".json"):
                affinity_candidate = filename
        
        if cif_candidate:
            if cif_candidate.endswith(".cif"):
                results['cif'] = zf.read(cif_candidate).decode('utf-8')
            elif cif_candidate.endswith(".pdb"):
                results['cif'] = zf.read(cif_candidate).decode('utf-8')
            
        if confidence_candidate:
            results['confidence'] = json.loads(zf.read(confidence_candidate))
        if affinity_candidate:
            results['affinity'] = json.loads(zf.read(affinity_candidate))
            
    if 'cif' not in results or 'confidence' not in results:
        raise FileNotFoundError("未能从结果文件中找到预测的结构 (.cif/.pdb) 或置信度信息 (.json)。")
    return results, raw_zip_bytes

def generate_yaml_from_state():
    """
    Generates the YAML configuration string based on the current session state.
    """
    if not st.session_state.get('components'):
        return None
        
    sequences_list = []
    chain_letters = string.ascii_uppercase + string.ascii_lowercase + string.digits
    next_letter_idx = 0
    
    for comp in st.session_state.components:
        num_copies = comp.get('num_copies', 1)
        current_ids = []
        for j in range(num_copies):
            if next_letter_idx + j < len(chain_letters):
                current_ids.append(chain_letters[next_letter_idx + j])
            else:
                st.warning(f"警告: 拷贝数过多，链ID可能重复或不足。请减少拷贝数或调整代码。")
                current_ids.append(f"UNK_{j}")
        next_letter_idx += num_copies
        
        component_dict = {'id': current_ids if len(current_ids) > 1 else current_ids[0]}

        if comp['type'] in ['protein', 'dna', 'rna']:
            component_dict['sequence'] = comp['sequence']
            if comp['type'] == 'protein' and comp.get('cyclic', False):
                component_dict['cyclic'] = True
            if comp['type'] == 'protein' and not st.session_state.use_msa_server:
                component_dict['msa'] = 'empty'
        elif comp['type'] == 'ligand':
            component_dict[comp['input_method']] = comp['sequence']
            
        sequences_list.append({comp['type']: component_dict})
        
    if not sequences_list:
        return None
        
    final_yaml_dict = {'version': 1, 'sequences': sequences_list}
    
    if st.session_state.properties.get('affinity') and st.session_state.properties.get('binder'):
        final_yaml_dict['properties'] = [{'affinity': {'binder': st.session_state.properties['binder']}}]
        
    return yaml.dump(final_yaml_dict, sort_keys=False, indent=2, default_flow_style=False)

def validate_inputs(components):
    """验证用户输入是否完整且有效。"""
    if not components:
        return False, "请至少添加一个组分。"
    for i, comp in enumerate(components):
        if not comp.get('sequence', '').strip():
            display_name = TYPE_TO_DISPLAY.get(comp.get('type', 'Unknown'), 'Unknown')
            return False, f"错误: 组分 {i+1} ({display_name}) 的序列不能为空。"
        
        if comp.get('type') == 'ligand' and comp.get('input_method') == 'smiles':
            smiles_str = comp.get('sequence', '').strip()
            if smiles_str and not all(c in string.printable for c in smiles_str):
                return False, f"错误: 组分 {i+1} (小分子) 的 SMILES 字符串包含非法字符。"
            
    if st.session_state.properties.get('affinity'):
        has_ligand_component_with_sequence = any(comp['type'] == 'ligand' and comp.get('sequence', '').strip() for comp in components)
        if not has_ligand_component_with_sequence:
            return False, "已选择计算亲和力，但未提供任何小分子序列。"
        if not st.session_state.properties.get('binder'):
            return False, "已选择计算亲和力，但未选择结合体（Binder）链ID。"
            
    return True, ""

st.set_page_config(layout="centered", page_title="Boltz-WebUI", page_icon="🧬")

if 'components' not in st.session_state: st.session_state.components = []
if 'task_id' not in st.session_state: st.session_state.task_id = None
if 'results' not in st.session_state: st.session_state.results = None
if 'raw_zip' not in st.session_state: st.session_state.raw_zip = None
if 'error' not in st.session_state: st.session_state.error = None
if 'properties' not in st.session_state: st.session_state.properties = {'affinity': False, 'binder': None}
if 'use_msa_server' not in st.session_state: st.session_state.use_msa_server = False

if not st.session_state.components:
    st.session_state.components.append({
        'id': str(uuid.uuid4()), 'type': 'protein', 'num_copies': 1, 'sequence': '', 'input_method': 'smiles', 'cyclic': False # Initialize cyclic to False
    })

st.markdown(f"""
<style>
    .stApp {{
        background-color: #FFFFFF;
        font-family: 'Segoe UI', 'Roboto', sans-serif;
    }}
    div.block-container {{
        padding-top: 2rem;
        padding-bottom: 2rem;
        max-width: 1024px;
    }}
    h1 {{
        color: #0053D6;
        text-align: left;
        margin-bottom: 0.5rem;
    }}
    h3 {{
        color: #555555;
        text-align: left;
    }}
    h2, h3, h4 {{
        color: #333333;
        margin-top: 1.5rem;
        margin-bottom: 0.8rem;
    }}
    .stButton>button {{
        border-radius: 8px;
        padding: 0.6rem 1.2rem;
        font-size: 1rem;
        font-weight: 500;
    }}
    .stButton>button[kind="primary"] {{
        background-color: #007bff;
        color: white;
        border: none;
    }}
    .stButton>button[kind="primary"]:hover {{
        background-color: #0056b3;
    }}
    .stButton>button[kind="secondary"] {{
        background-color: #f0f2f6;
        color: #333333;
        border: 1px solid #ddd;
    }}
    .stButton>button[kind="secondary"]:hover {{
        background-color: #e0e0e0;
    }}
    .stButton>button[data-testid="baseButton-secondary"] {{
        border: none !important;
        background-color: transparent !important;
        color: #888 !important;
        padding: 0 !important;
        font-size: 1.2rem;
    }}
    .stButton>button[data-testid="baseButton-secondary"]:hover {{
        color: #ff4b4b !important;
    }}
    div[data-testid="stCheckbox"] {{
        display: flex;
        align-items: center;
        margin-top: 10px;
        margin-bottom: 10px;
    }}
    .stExpander {{
        border: 1px solid #e6e6e6;
        border-radius: 8px;
        box-shadow: 0 2px 4px rgba(0,0,0,0.05);
        margin-bottom: 1.5rem;
    }}
    .stExpander>div>div[data-testid="stExpanderToggleIcon"] {{
        font-size: 1.5rem;
    }}
    .stCode {{
        background-color: #f8f8f8;
        border-left: 5px solid #007bff;
        padding: 10px;
        border-radius: 5px;
    }}
    .stAlert {{
        border-radius: 8px;
    }}
    hr {{
        border-top: 1px solid #eee;
        margin-top: 1.5rem;
        margin-bottom: 1.5rem;
    }}
    
    .loader {{
      border: 6px solid #f3f3f3;
      border-top: 6px solid #007bff;
      border-radius: 50%;
      width: 50px;
      height: 50px;
      animation: spin 1.5s linear infinite;
      margin: 20px auto;
    }}

    @keyframes spin {{
      0% {{ transform: rotate(0deg); }}
      100% {{ transform: rotate(360deg); }}
    }}
</style>
""", unsafe_allow_html=True)

st.title("🧬 Boltz-WebUI")
st.markdown("蛋白质-分子复合物结构预测工具。")
st.divider()

is_running = (
    st.session_state.task_id is not None and st.session_state.results is None and st.session_state.error is None
)

with st.expander("🔧 **步骤 1: 配置您的预测任务**", expanded=not is_running and not st.session_state.results):
    st.markdown("填写以下信息，定义您希望预测的生物大分子和小分子组分。")
    id_to_delete = None
    
    for i, component in enumerate(st.session_state.components[:]):
        st.markdown(f"---")
        st.subheader(f"组分 {i+1}", anchor=False)
        
        cols_type_copies = st.columns([3, 1])
        type_options = list(TYPE_TO_DISPLAY.keys())
        current_type = component.get('type', 'protein')
        current_type_index = type_options.index(current_type)
        
        selected_type = cols_type_copies[0].selectbox(
            "选择组分类型", options=type_options, format_func=lambda x: TYPE_TO_DISPLAY[x],
            key=f"type_{component['id']}", index=current_type_index, disabled=is_running,
            help="选择此组分是蛋白质、DNA、RNA 还是小分子。"
        )

        if selected_type != current_type:
            st.session_state.components[i]['sequence'] = ''
            st.session_state.components[i]['type'] = selected_type
            # Reset cyclic for new type if changed from protein
            if selected_type != 'protein':
                st.session_state.components[i]['cyclic'] = False
            st.rerun()

        st.session_state.components[i]['num_copies'] = cols_type_copies[1].number_input(
            "拷贝数", min_value=1, max_value=20, step=1, key=f"copies_{component['id']}",
            value=component.get('num_copies', 1), disabled=is_running,
            help="如果您有多个相同类型的分子，可以在这里设置拷贝数，系统会为每个拷贝生成一个唯一的链ID。"
        )

        if selected_type == 'ligand':
            method_options = ["smiles", "ccd"]
            current_method_index = method_options.index(component.get('input_method', 'smiles'))
            
            st.session_state.components[i]['input_method'] = st.radio(
                "小分子输入方式", method_options, key=f"ligand_type_{component['id']}",
                index=current_method_index, disabled=is_running, horizontal=True,
                help="选择通过SMILES字符串（结构描述符）或CCD ID（化合物组件字典ID）输入小分子。"
            )
            
            if st.session_state.components[i]['input_method'] == 'smiles':
                initial_smiles = st.session_state.components[i].get('sequence', '')
                
                st.info("🎨 在下方 **Ketcher 编辑器** 中绘制分子，或直接粘贴 SMILES 字符串。**编辑完成后，请点击编辑器内部的 'Apply' 按钮，SMILES 字符串将自动更新。**", icon="💡")
                
                ketcher_current_smiles = st_ketcher(
                    value=initial_smiles,
                    key=f"ketcher_{component['id']}",
                    height=400
                )
                
                if ketcher_current_smiles is not None and ketcher_current_smiles != initial_smiles:
                    st.session_state.components[i]['sequence'] = ketcher_current_smiles
                    if ketcher_current_smiles:
                        st.toast("✅ SMILES 字符串已成功更新！", icon="🧪")
                    
                st.markdown("<hr style='margin-top: 0.5rem; margin-bottom: 0.5rem'>", unsafe_allow_html=True)
                st.caption("✨ Ketcher 生成的 SMILES 字符串:")
                current_smiles_in_state = st.session_state.components[i].get('sequence', '')
                if current_smiles_in_state:
                    st.code(current_smiles_in_state, language='smiles')
                else:
                    st.info("👆 请开始绘制或粘贴，SMILES 将会显示在这里。")

            else:
                st.session_state.components[i]['sequence'] = st.text_input(
                    f"输入 {st.session_state.components[i].get('input_method', 'ccd').upper()} (例如: NAG)", key=f"seq_{component['id']}",
                    value=component.get('sequence', ''), 
                    placeholder="输入CCD ID，如 'HEM', 'ADP'", disabled=is_running,
                    help="输入标准化合物组件字典（CCD）中的三字母或多字母代码。"
                )
        else:
            placeholder_text = TYPE_SPECIFIC_INFO.get(selected_type, {}).get('placeholder', '')
            help_text = TYPE_SPECIFIC_INFO.get(selected_type, {}).get('help', '')
            st.session_state.components[i]['sequence'] = st.text_area(
                f"输入 {selected_type.capitalize()} 序列", height=120, key=f"seq_{component['id']}",
                value=component.get('sequence', ''),
                placeholder=placeholder_text,
                help=help_text,
                disabled=is_running
            )
            
            # Add cyclic peptide option for protein type
            if selected_type == 'protein':
                st.session_state.components[i]['cyclic'] = st.checkbox(
                    "环肽 (Cyclic Peptide)",
                    value=st.session_state.components[i].get('cyclic', False),
                    key=f"cyclic_{component['id']}",
                    help="勾选此项表示该蛋白质序列是一个环状肽。对于环肽，模型将尝试生成闭合的环状结构。",
                    disabled=is_running
                )
        
        delete_col, _ = st.columns([10, 1])
        with delete_col:
            if len(st.session_state.components) > 1:
                if st.button("🗑️ 删除此组分", key=f"del_{component['id']}", help="从任务中移除此组分", disabled=is_running):
                    id_to_delete = component['id']
        
    if id_to_delete:
        st.session_state.components = [c for c in st.session_state.components if c.get('id') != id_to_delete]
        st.rerun()

    st.markdown("---")
    st.button("➕ 添加新组分", on_click=lambda: st.session_state.components.append({'id': str(uuid.uuid4()), 'type': 'protein', 'num_copies': 1, 'sequence': '', 'input_method': 'smiles', 'cyclic': False}), disabled=is_running, use_container_width=True)

    st.subheader("全局与高级设置", anchor=False)

    st.session_state.use_msa_server = st.checkbox(
        "启用 MSA 序列搜索 (推荐用于蛋白质)",
        value=st.session_state.get('use_msa_server', False),
        help="勾选此项将使用外部服务器为蛋白质序列生成多序列比对(MSA)。这可以显著提升对新颖蛋白质的预测精度，但会增加任务耗时。",
        disabled=is_running
    )
    
    has_ligand_component = any(comp['type'] == 'ligand' for comp in st.session_state.components)
    if has_ligand_component:
        st.session_state.properties['affinity'] = st.checkbox(
            "🔬 计算结合亲和力 (Affinity)",
            value=st.session_state.properties.get('affinity', False),
            disabled=is_running,
            help="勾选后，模型将尝试预测小分子与大分子组分之间的结合亲和力。请确保至少输入了一个小分子组分。"
        )
        if st.session_state.properties['affinity']:
            chain_letter_idx = 0
            valid_ligand_chains = []
            for comp in st.session_state.components:
                if comp.get('sequence', '').strip():
                    num_copies = comp.get('num_copies', 1)
                    if comp['type'] == 'ligand':
                        for j in range(num_copies):
                            if (chain_letter_idx + j) < len(string.ascii_uppercase):
                                chain_id = string.ascii_uppercase[(chain_letter_idx + j)]
                                valid_ligand_chains.append(chain_id)
                            else:
                                pass
                    chain_letter_idx += num_copies
            
            if valid_ligand_chains:
                current_binder = st.session_state.properties.get('binder')
                try:
                    binder_index = valid_ligand_chains.index(current_binder)
                except ValueError:
                    binder_index = 0 if valid_ligand_chains else -1
                
                if binder_index != -1:
                    st.session_state.properties['binder'] = st.selectbox(
                        "选择作为“结合体(Binder)”的小分子链 ID",
                        options=valid_ligand_chains,
                        index=binder_index,
                        help="被选中的小分子链将被视为与其余所有链形成复合物的结合伙伴。预测结果将围绕此结合事件进行评估。",
                        disabled=is_running
                    )
                else:
                    st.session_state.properties['binder'] = None
                    st.warning("请为至少一个小分子组分输入序列(SMILES/CCD)以选择结合体。", icon="⚠️")
            else:
                st.session_state.properties['binder'] = None
                st.warning("请为至少一个小分子组分输入序列(SMILES/CCD)以选择结合体。", icon="⚠️")
    else:
        if 'properties' in st.session_state:
            st.session_state.properties['affinity'] = False
            st.session_state.properties['binder'] = None

is_valid, validation_message = validate_inputs(st.session_state.components)
yaml_preview = generate_yaml_from_state() if is_valid else None

if st.button("🚀 提交预测任务", type="primary", disabled=(not is_valid or is_running), use_container_width=True):
    st.session_state.task_id = None
    st.session_state.results = None
    st.session_state.raw_zip = None
    st.session_state.error = None
    
    with st.spinner("⏳ 正在提交任务，请稍候..."):
        try:
            task_id = submit_job(
                yaml_content=yaml_preview,
                use_msa=st.session_state.use_msa_server
            )
            st.session_state.task_id = task_id
            st.toast(f"🎉 任务已成功提交！任务ID: {task_id}", icon="✅")
            st.rerun()
        except requests.exceptions.RequestException as e:
            st.error(f"⚠️ **任务提交失败：无法连接到API服务器或服务器返回错误**。请检查后端服务是否运行正常。详情: {e}")
            st.session_state.error = {"error_message": str(e), "type": "API Connection Error"}
        except Exception as e:
            st.error(f"❌ **任务提交失败：发生未知错误**。详情: {e}")
            st.session_state.error = {"error_message": str(e), "type": "Client Error"}


if not is_valid and not is_running:
    st.error(f"⚠️ **无法提交**: {validation_message}")

if st.session_state.task_id and not st.session_state.results:
    st.divider()
    st.header("✨ **步骤 2: 查看预测结果**", anchor=False)
    if not st.session_state.error:
        spinner_and_status_placeholder = st.empty()
        
        while True:
            try:
                status_data = get_status(st.session_state.task_id)
                current_state = status_data.get('state', 'UNKNOWN')
                
                with spinner_and_status_placeholder.container():
                    if current_state == 'SUCCESS':
                        st.success("🎉 任务成功完成！正在下载并渲染结果...")
                        try:
                            processed_results, raw_zip_bytes = download_and_process_results(st.session_state.task_id)
                            st.session_state.results = processed_results
                            st.session_state.raw_zip = raw_zip_bytes
                            st.toast("✅ 结果已成功加载！", icon="🎊")
                            st.rerun()
                            break 
                        except (FileNotFoundError, json.JSONDecodeError) as e:
                            st.session_state.error = {"error_message": f"处理结果文件失败：{e}", "type": "Result File Error"}
                            st.error(f"❌ **结果文件处理失败**：{e}")
                            break
                        except requests.exceptions.RequestException as e:
                            st.session_state.error = {"error_message": f"下载结果文件失败：{e}", "type": "Download Error"}
                            st.error(f"❌ **下载结果文件失败**：{e}")
                            break
                    elif current_state == 'FAILURE':
                        st.session_state.error = status_data.get('info', {})
                        error_message = st.session_state.error.get('exc_message', '未知错误')
                        st.error(f"❌ **任务失败**：{error_message}")
                        break
                    elif current_state == 'PENDING':
                        st.markdown('<div class="loader"></div>', unsafe_allow_html=True)
                        st.info("🕒 任务正在排队中，请耐心等待...")
                    elif current_state == 'STARTED' or current_state == 'PROGRESS':
                        info_message = status_data.get('info', {}).get('message', f"当前状态: **{current_state}**")
                        st.markdown('<div class="loader"></div>', unsafe_allow_html=True)
                        st.info(f"🔬 **任务正在运行**：{info_message} (页面将每 10 秒自动刷新)", icon="⏳")
                    else:
                        st.markdown('<div class="loader"></div>', unsafe_allow_html=True)
                        st.warning(f"❓ 任务状态未知或正在初始化... (当前状态: **{current_state}**)")

                time.sleep(10)
            except requests.exceptions.RequestException as e:
                spinner_and_status_placeholder.error(f"🚨 **无法获取任务状态：API连接失败**。请检查后端服务是否运行正常。详情: {e}")
                st.session_state.error = {"error_message": str(e), "type": "API Connection Error"}
                break
            except Exception as e:
                spinner_and_status_placeholder.error(f"🚨 **获取任务状态时发生未知错误**。详情: {e}")
                st.session_state.error = {"error_message": str(e), "type": "Client Error"}
                break

if st.session_state.error:
    st.error("ℹ️ 任务执行失败，详细信息如下：")
    st.json(st.session_state.error)
    if st.button("🔄 重置并重新开始", type="secondary"):
        for key in ['task_id', 'results', 'raw_zip', 'error', 'components', 'properties', 'use_msa_server']:
            if key in st.session_state:
                del st.session_state[key]
        st.rerun()

if st.session_state.results:
    st.divider()
    st.header("✅ **步骤 2: 预测结果展示**", anchor=False)

    cif_data = st.session_state.results.get('cif', '')
    confidence_data = st.session_state.results.get('confidence', {})
    affinity_data = st.session_state.results.get('affinity', {})

    col_vis, col_data = st.columns([3, 2])

    with col_vis:
        st.subheader("📊 3D 结构可视化", anchor=False)
        if cif_data:
            try:
                structure = read_cif_from_string(cif_data)
                protein_bfactors = extract_protein_residue_bfactors(structure)
                
                with st.expander("⚙️ **视图设置**", expanded=True):
                    row1_col1, row1_col2 = st.columns(2)
                    with row1_col1:
                        st.selectbox("大分子样式", ['cartoon', 'stick', 'sphere'], key='protein_style_vis', help="选择蛋白质、DNA、RNA 等大分子的渲染样式。", index=0)
                    with row1_col2:
                        st.selectbox(
                            "着色方案",
                            ['pLDDT', 'Chain', 'Rainbow', 'Secondary Structure'],
                            key='color_scheme_vis',
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
                        st.selectbox("配体样式", ['ball-and-stick', 'space-filling', 'stick', 'line'], key='ligand_style_vis', help="选择小分子的渲染样式。", index=0)
                    with row2_col2:
                        st.checkbox("🔄 旋转模型", key='spin_model_vis', value=False, help="勾选后，模型将自动围绕Z轴旋转。")
                
                view_html = visualize_structure_py3dmol(
                    cif_content=cif_data,
                    residue_bfactors=protein_bfactors,
                    protein_style=st.session_state.protein_style_vis,
                    ligand_style=st.session_state.ligand_style_vis,
                    spin=st.session_state.spin_model_vis,
                    color_scheme=st.session_state.color_scheme_vis
                )
                st.components.v1.html(view_html, height=600, scrolling=False)
            except Exception as e:
                st.error("加载 3D 结构时发生错误。请尝试刷新页面或检查输入数据。", icon="❌")
                st.exception(e)
        else:
            st.warning("未能从结果中提取出有效的分子结构文件 (.cif/.pdb)，无法显示 3D 结构。", icon="⚠️")

    with col_data:
        st.subheader("📈 预测质量与亲和力评估", anchor=False)
        st.markdown("---")

        st.markdown("<b>pLDDT 置信度图例</b>", unsafe_allow_html=True)
        st.markdown("""
        <div style='display: flex; flex-wrap: wrap; gap: 10px; margin-top: 5px; margin-bottom: 25px;'>
            <div style='display: flex; align-items: center;'><div style='width: 15px; height: 15px; background-color: #0053D6; border-radius: 3px; margin-right: 5px;'></div><span><b>极高</b> (> 90)</span></div>
            <div style='display: flex; align-items: center;'><div style='width: 15px; height: 15px; background-color: #65CBF3; border-radius: 3px; margin-right: 5px;'></div><span><b>高</b> (70-90)</span></div>
            <div style='display: flex; align-items: center;'><div style='width: 15px; height: 15px; background-color: #FFDB13; border-radius: 3px; margin-right: 5px;'></div><span><b>中等</b> (50-70)</span></div>
            <div style='display: flex; align-items: center;'><div style='width: 15px; height: 15px; background-color: #FF7D45; border-radius: 3px; margin-right: 5px;'></div><span><b>低</b> (&lt; 50)</span></div>
        </div>
        """, unsafe_allow_html=True)
        
        st.markdown("<b>全局结构质量指标</b>", unsafe_allow_html=True)
        cols_metrics = st.columns(2)
        cols_metrics[0].metric(
            "平均 pLDDT",
            f"{confidence_data.get('complex_plddt', 0):.2f}",
            help="预测的局部距离差异检验 (pLDDT) 是一个 0-100 范围内的单残基置信度得分，代表模型对局部结构预测的信心。这是整个复合物所有残基的平均 pLDDT 分数。值越高越好。"
        )
        cols_metrics[1].metric(
            "pTM",
            f"{confidence_data.get('ptm', 0):.4f}",
            help="预测的模板建模评分 (pTM) 是一个 0-1 范围内的分数，用于衡量预测结构与真实结构在全局拓扑结构上的相似性。pTM > 0.5 通常表示预测了正确的折叠方式。值越高越好。"
        )
        cols_metrics[0].metric(
            "ipTM",
            f"{confidence_data.get('iptm', 0):.4f}",
            help="界面预测模板建模评分 (ipTM) 是专门用于评估链间相互作用界面准确性的指标 (0-1)。ipTM > 0.85 通常表明对复合物的相互作用方式有很高的置信度。值越高越好。"
        )
        cols_metrics[1].metric(
            "PAE (Å)",
            f"{confidence_data.get('complex_pde', 0):.2f}",
            help="预测的对齐误差 (PAE) 表示残基对之间的预期位置误差（单位为埃 Å）。较低的值表示对不同结构域和链的相对位置和方向有更高的信心。这里显示的是整个复合物的平均误差。值越低越好。"
        )
        
        if affinity_data and st.session_state.properties.get('affinity'):
            st.markdown("<br><b>亲和力预测指标</b>", unsafe_allow_html=True)
            log_ic50_in_uM = affinity_data.get("affinity_pred_value")
            if log_ic50_in_uM is not None:
                ic50_uM = math.pow(10, log_ic50_in_uM)
                if ic50_uM > 1000:
                    display_ic50 = f"{ic50_uM/1000:.3f} mM"
                elif ic50_uM > 1000000:
                     display_ic50 = f"{ic50_uM/1000000:.3f} M"
                else:
                    display_ic50 = f"{ic50_uM:.3f} µM"
                
                pIC50 = 6 - log_ic50_in_uM
                delta_g = -1.364 * pIC50
                
                st.metric("预测 IC50", display_ic50, help="预测的半数抑制浓度 (IC50) 是指结合体（Binder）抑制其靶标 50% 所需的浓度。它是衡量效力的常用指标，数值越低表示预测的亲和力越强。")
                affinity_cols = st.columns(2)
                affinity_cols[0].metric("预测 pIC50", f"{pIC50:.3f}", help="pIC50 是 IC50 值的负对数 (pIC50 = -log10(IC50 in M))。这个标度更便于比较，数值越高表示预测的亲和力越强。")
                affinity_cols[1].metric("结合自由能 (ΔG)", f"{delta_g:.3f} kcal/mol", help="预测的吉布斯自由能 (ΔG) 反映了结合事件的自发性，由 pIC50 计算得出。负值越大，表明结合作用越强、越有利。")
            binder_prob = affinity_data.get("affinity_probability_binary")
            if binder_prob is not None:
                st.metric("结合概率", f"{binder_prob:.2%}", help="模型预测“结合体”与其余组分形成稳定复合物的概率。百分比越高，表明模型对这是一个真实的结合事件越有信心。")
        else:
            st.info("💡 如需亲和力预测结果，请在步骤1中勾选 **计算结合亲和力 (Affinity)** 选项。", icon="ℹ️")


        st.markdown("---")
        st.markdown("<b>📥 下载结果文件</b>", unsafe_allow_html=True)
        if st.session_state.get("raw_zip"):
            st.download_button(
                label="📥 下载所有结果 (ZIP)",
                data=st.session_state.raw_zip,
                file_name=f"boltz_results_{st.session_state.task_id}.zip",
                mime="application/zip",
                use_container_width=True,
                type="primary",
                help="下载包含所有预测结果（CIF、JSON指标等）的原始ZIP文件。"
            )
        
        cols_download = st.columns(2)
        with cols_download[0]:
            if cif_data:
                st.download_button("📥 下载 PDB", export_to_pdb(cif_data), "predicted_structure.pdb", "chemical/x-pdb", use_container_width=True, help="下载预测结构的PDB格式文件。")
        with cols_download[1]:
             if cif_data:
                st.download_button("📄 下载 CIF", cif_data, "predicted_structure.cif", "chemical/x-cif", use_container_width=True, help="下载预测结构的CIF格式文件。")
        
        all_json_data = {"confidence": confidence_data, "affinity": affinity_data}
        st.download_button(
            label="📦 下载指标数据 (JSON)",
            data=json.dumps(all_json_data, indent=2),
            file_name="prediction_metrics.json",
            mime="application/json",
            use_container_width=True,
            help="下载包含pLDDT、pTM、ipTM、PAE以及亲和力预测结果的JSON文件。"
        )