
import streamlit as st
import sys
import os

# Add the project root to the Python path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.append(PROJECT_ROOT)

from frontend.state import initialize_session_state
from frontend.views.prediction_page import render_prediction_page
from frontend.views.designer_page import render_designer_page
from frontend.views.affinity_page import render_affinity_page
from frontend.url_state import URLStateManager

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
        width: auto;
        max-width: 300px;
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

# 创建选项卡
tab1, tab2, tab3 = st.tabs(["结构预测", "分子设计", "亲和力预测"])

with tab1:
    render_prediction_page()

with tab2:
    render_designer_page()

with tab3:
    render_affinity_page()
