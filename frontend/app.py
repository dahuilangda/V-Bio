
import streamlit as st
import sys
import os
from pathlib import Path

# Add the project root to the Python path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.append(PROJECT_ROOT)

# Load .env file if it exists
def load_env_file():
    """Load environment variables from .env file"""
    env_file = Path(PROJECT_ROOT) / ".env"
    if env_file.exists():
        try:
            with open(env_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, value = line.split('=', 1)
                        if key not in os.environ:
                            os.environ[key.strip()] = value.strip()
            print(f"✅ Loaded .env file from {env_file}")
        except Exception as e:
            print(f"⚠️ Warning: Failed to load .env file: {e}")

# Load environment variables
load_env_file()

from frontend.state import initialize_session_state
from frontend.views.prediction_page import render_prediction_page
from frontend.views.peptide_designer_page import render_peptide_designer_page
from frontend.views.lead_optimization_page import render_lead_optimization_page
from frontend.views.affinity_page import render_affinity_page

st.set_page_config(layout="centered", page_title="Boltz-WebUI", page_icon="🧬")

initialize_session_state()

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
    
    .stTabs [data-baseweb="tab-list"] {{
        gap: 0;
        background: transparent;
        padding: 0;
        border-radius: 0;
        margin-bottom: 1.5rem;
        box-shadow: none;
        border-bottom: 2px solid #f1f5f9;
        justify-content: flex-start;
        width: 500px;
        max-width: none;
    }}
    
    .stTabs [data-baseweb="tab"] {{
        height: 40px;
        background: transparent;
        border-radius: 0;
        color: #64748b;
        font-weight: 500;
        font-size: 14px;
        border: none;
        padding: 0 16px;
        transition: all 0.2s ease;
        position: relative;
        display: flex;
        align-items: center;
        justify-content: center;
        min-width: auto;
        border-bottom: 2px solid transparent;
    }}
    
    .stTabs [data-baseweb="tab"]:hover:not([aria-selected="true"]) {{
        color: #374151;
        background: #f8fafc;
    }}
    
    .stTabs [aria-selected="true"] {{
        background: transparent !important;
        color: #1e293b !important;
        border-bottom: 2px solid #3b82f6 !important;
        font-weight: 600 !important;
    }}
    
    .stTabs [data-baseweb="tab"]::before,
    .stTabs [data-baseweb="tab"]::after {{
        display: none;
    }}
</style>
""", unsafe_allow_html=True)

st.title("🧬 Boltz-WebUI")
st.markdown("蛋白质-分子复合物结构预测与设计平台")

query_params = st.query_params
task_type_param = query_params.get("task_type", "prediction")
if isinstance(task_type_param, list):
    task_type_param = task_type_param[0] if task_type_param else "prediction"
task_type = str(task_type_param)

task_type_to_tab_index = {
    "prediction": 0,
    "peptide_designer": 1,
    "peptide_design": 1,
    "designer": 1,
    "bicyclic_designer": 1,
    "lead_optimization": 2,
    "affinity": 3,
}
target_tab_index = task_type_to_tab_index.get(task_type, 0)

# 创建选项卡（保持原有样式）
tab1, tab2, tab3, tab4 = st.tabs(["结构预测", "多肽设计", "先导优化", "亲和力预测"])

# URL 驱动选项卡：根据 task_type 自动切换；点击时同步更新 task_type 到地址栏
st.components.v1.html(f"""
<script>
(() => {{
    const parentWin = window.parent || window;
    const root = parentWin.document;
    const targetIndex = {target_tab_index};
    const indexToTaskType = {{
        0: "prediction",
        1: "peptide_designer",
        2: "lead_optimization",
        3: "affinity",
    }};

    const getTabs = () => {{
        let tabs = root.querySelectorAll('[data-baseweb="tab"]');
        if (!tabs.length) tabs = root.querySelectorAll('button[role="tab"]');
        if (!tabs.length) tabs = root.querySelectorAll('.stTabs button');
        return tabs;
    }};

    const bindAndSwitch = () => {{
        const tabs = getTabs();
        if (!tabs.length) return false;

        tabs.forEach((tab, idx) => {{
            if (tab.dataset.boltzTaskTypeBound === "1") return;
            tab.dataset.boltzTaskTypeBound = "1";
            tab.addEventListener("click", () => {{
                const url = new URL(parentWin.location.href);
                url.searchParams.set("task_type", indexToTaskType[idx] || "prediction");
                parentWin.history.replaceState({{}}, "", url.toString());
            }});
        }});

        if (tabs.length > targetIndex) {{
            const targetTab = tabs[targetIndex];
            if (targetTab && targetTab.getAttribute("aria-selected") !== "true") {{
                targetTab.click();
            }}
        }}
        return true;
    }};

    let attempts = 0;
    const maxAttempts = 20;
    const timer = parentWin.setInterval(() => {{
        attempts += 1;
        if (bindAndSwitch() || attempts >= maxAttempts) {{
            parentWin.clearInterval(timer);
        }}
    }}, 120);
}})();
</script>
""", height=0)

with tab1:
    render_prediction_page()

with tab2:
    render_peptide_designer_page()

with tab3:
    render_lead_optimization_page()

with tab4:
    render_affinity_page()
