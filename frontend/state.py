
import streamlit as st
import uuid

def initialize_session_state():
    """Initializes all the necessary session state variables."""
    if 'components' not in st.session_state: st.session_state.components = []
    if 'constraints' not in st.session_state: st.session_state.constraints = []
    if 'task_id' not in st.session_state: st.session_state.task_id = None
    if 'results' not in st.session_state: st.session_state.results = None
    if 'raw_zip' not in st.session_state: st.session_state.raw_zip = None
    if 'error' not in st.session_state: st.session_state.error = None
    if 'properties' not in st.session_state: st.session_state.properties = {'affinity': False, 'binder': None}
    if 'use_msa_server' not in st.session_state: st.session_state.use_msa_server = False

    # Designer-related session state
    if 'designer_task_id' not in st.session_state: st.session_state.designer_task_id = None
    if 'designer_work_dir' not in st.session_state: st.session_state.designer_work_dir = None
    if 'designer_results' not in st.session_state: st.session_state.designer_results = None
    if 'designer_error' not in st.session_state: st.session_state.designer_error = None
    if 'designer_config' not in st.session_state: st.session_state.designer_config = {}

    if not st.session_state.components:
        st.session_state.components.append({
            'id': str(uuid.uuid4()), 'type': 'protein', 'num_copies': 1, 'sequence': '', 'input_method': 'smiles', 'cyclic': False, 'use_msa': False
        })
