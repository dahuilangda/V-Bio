import streamlit as st
import uuid

def initialize_session_state():
    """Initializes all the necessary session state variables."""
    # Basic prediction state
    if 'components' not in st.session_state: st.session_state.components = []
    if 'constraints' not in st.session_state: st.session_state.constraints = []
    if 'task_id' not in st.session_state: st.session_state.task_id = None
    if 'results' not in st.session_state: st.session_state.results = None
    if 'raw_zip' not in st.session_state: st.session_state.raw_zip = None
    if 'error' not in st.session_state: st.session_state.error = None
    if 'properties' not in st.session_state: st.session_state.properties = {'affinity': False, 'binder': None}
    if 'use_msa_server' not in st.session_state: st.session_state.use_msa_server = False
    if 'prediction_backend' not in st.session_state: st.session_state.prediction_backend = 'boltz'
    if 'designer_backend' not in st.session_state: st.session_state.designer_backend = 'boltz'

    # Designer-related session state
    if 'designer_task_id' not in st.session_state: st.session_state.designer_task_id = None
    if 'designer_work_dir' not in st.session_state: st.session_state.designer_work_dir = None
    if 'designer_results' not in st.session_state: st.session_state.designer_results = None
    if 'designer_error' not in st.session_state: st.session_state.designer_error = None
    if 'designer_config' not in st.session_state: st.session_state.designer_config = {}

    # Bicyclic Designer-related session state
    if 'bicyclic_task_id' not in st.session_state: st.session_state.bicyclic_task_id = None
    if 'bicyclic_work_dir' not in st.session_state: st.session_state.bicyclic_work_dir = None
    if 'bicyclic_results' not in st.session_state: st.session_state.bicyclic_results = None
    if 'bicyclic_error' not in st.session_state: st.session_state.bicyclic_error = None
    if 'bicyclic_config' not in st.session_state: st.session_state.bicyclic_config = {}
    if 'bicyclic_backend' not in st.session_state: st.session_state.bicyclic_backend = 'boltz'
    if 'bicyclic_components' not in st.session_state: st.session_state.bicyclic_components = [
        {'id': str(uuid.uuid4()), 'type': 'protein', 'sequence': '', 'num_copies': 1, 'use_msa': False}
    ]
    if 'bicyclic_constraints' not in st.session_state: st.session_state.bicyclic_constraints = []
    
    # Affinity-related session state
    if 'affinity_task_id' not in st.session_state: st.session_state.affinity_task_id = None
    if 'affinity_results' not in st.session_state: st.session_state.affinity_results = None
    if 'affinity_error' not in st.session_state: st.session_state.affinity_error = None
    if 'ligand_resnames' not in st.session_state: st.session_state.ligand_resnames = []
    if 'affinity_cif' not in st.session_state: st.session_state.affinity_cif = None

    # Lead optimization-related session state
    if 'lead_optimization_task_id' not in st.session_state: st.session_state.lead_optimization_task_id = None
    if 'lead_optimization_results' not in st.session_state: st.session_state.lead_optimization_results = None
    if 'lead_optimization_error' not in st.session_state: st.session_state.lead_optimization_error = None
    if 'lead_optimization_raw_zip' not in st.session_state: st.session_state.lead_optimization_raw_zip = None
    if 'lead_optimization_constraints' not in st.session_state: st.session_state.lead_optimization_constraints = []
    if 'lead_optimization_backend' not in st.session_state: st.session_state.lead_optimization_backend = 'boltz'
    if 'lead_opt_pair_chain_a' not in st.session_state: st.session_state.lead_opt_pair_chain_a = 'B'
    if 'lead_opt_pair_chain_b' not in st.session_state: st.session_state.lead_opt_pair_chain_b = 'A'
    if 'lead_opt_core_mode' not in st.session_state: st.session_state.lead_opt_core_mode = '不限制'
    if 'lead_opt_core_input_method' not in st.session_state: st.session_state.lead_opt_core_input_method = 'SMILES/SMARTS'
    if 'lead_opt_core_smarts' not in st.session_state: st.session_state.lead_opt_core_smarts = ''
    if 'lead_opt_core_ketcher_smiles' not in st.session_state: st.session_state.lead_opt_core_ketcher_smiles = ''
    if 'lead_opt_core_enabled' not in st.session_state: st.session_state.lead_opt_core_enabled = False
    if 'lead_opt_exclude_enabled' not in st.session_state: st.session_state.lead_opt_exclude_enabled = False
    if 'lead_opt_rgroup_enabled' not in st.session_state: st.session_state.lead_opt_rgroup_enabled = False
    if 'lead_opt_exclude_input_method' not in st.session_state: st.session_state.lead_opt_exclude_input_method = 'SMILES/SMARTS'
    if 'lead_opt_exclude_smarts' not in st.session_state: st.session_state.lead_opt_exclude_smarts = ''
    if 'lead_opt_exclude_ketcher_smiles' not in st.session_state: st.session_state.lead_opt_exclude_ketcher_smiles = ''
    if 'lead_opt_rgroup_input_method' not in st.session_state: st.session_state.lead_opt_rgroup_input_method = 'SMILES/SMARTS'
    if 'lead_opt_rgroup_smarts' not in st.session_state: st.session_state.lead_opt_rgroup_smarts = ''
    if 'lead_opt_rgroup_ketcher_smiles' not in st.session_state: st.session_state.lead_opt_rgroup_ketcher_smiles = ''
    if 'lead_opt_fragment_selections' not in st.session_state: st.session_state.lead_opt_fragment_selections = {}
    if 'lead_opt_fragment_smiles' not in st.session_state: st.session_state.lead_opt_fragment_smiles = []
    if 'lead_opt_fragment_source' not in st.session_state: st.session_state.lead_opt_fragment_source = ''
    if 'lead_opt_fragment_note' not in st.session_state: st.session_state.lead_opt_fragment_note = ''
    if 'lead_opt_fragment_selected' not in st.session_state: st.session_state.lead_opt_fragment_selected = ''
    if 'lead_opt_fragment_action' not in st.session_state: st.session_state.lead_opt_fragment_action = '不限制'
    if 'lead_opt_fragment_match_map' not in st.session_state: st.session_state.lead_opt_fragment_match_map = {}
    
    # URL state management
    if 'url_state_initialized' not in st.session_state: st.session_state.url_state_initialized = False

    if not st.session_state.components:
        st.session_state.components.append({
            'id': str(uuid.uuid4()), 'type': 'protein', 'num_copies': 1, 'sequence': '', 'input_method': 'smiles', 'cyclic': False, 'use_msa': False
        })
    
    # 从URL参数恢复状态（只在首次初始化时执行）
    if not st.session_state.url_state_initialized:
        try:
            from frontend.url_state import URLStateManager
            URLStateManager.restore_state_from_url()
            st.session_state.url_state_initialized = True
        except Exception as e:
            st.error(f"从URL恢复状态失败: {e}")
            st.session_state.url_state_initialized = True
    
    # 初始化选项卡切换跟踪变量
    if 'last_switched_url' not in st.session_state:
        st.session_state.last_switched_url = ''
