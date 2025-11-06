import streamlit as st
import urllib.parse
from typing import Dict, Optional, Any
import json

class URLStateManager:
    """
    URL状态管理器 - 负责在URL中保存和恢复应用状态
    支持预测任务和设计任务的状态持久化
    """
    
    @staticmethod
    def get_query_params() -> Dict[str, Any]:
        """获取当前URL中的查询参数"""
        query_params = st.query_params
        return dict(query_params)
    
    @staticmethod
    def set_query_params(**params):
        """设置URL查询参数"""
        # 清空现有参数并设置新参数
        for key, value in params.items():
            if value is not None:
                st.query_params[key] = str(value)
            elif key in st.query_params:
                del st.query_params[key]
    
    @staticmethod
    def update_url_for_prediction_task(task_id: str, task_type: str = "prediction", components=None, constraints=None, properties=None, backend: str = None):
        """为预测任务更新URL参数，包括配置信息"""
        import json
        params = {
            'task_id': task_id,
            'task_type': task_type
        }
        
        # 如果提供了配置信息，将其序列化存储
        if components:
            try:
                # 简化组件信息，只保存关键数据
                simplified_components = []
                for comp in components:
                    if comp.get('sequence', '').strip():  # 只保存有序列的组件
                        simplified_comp = {
                            'type': comp.get('type'),
                            'num_copies': comp.get('num_copies', 1),
                            'sequence': comp.get('sequence', ''),
                            'input_method': comp.get('input_method', 'smiles'),
                            'cyclic': comp.get('cyclic', False),
                            'use_msa': comp.get('use_msa', True)
                        }
                        simplified_components.append(simplified_comp)
                
                if simplified_components or constraints or properties or backend:
                    params['config'] = json.dumps({
                        'components': simplified_components,
                        'constraints': constraints or [],
                        'properties': properties or {},
                        'backend': backend or 'boltz'
                    })
            except Exception as e:
                # 如果序列化失败，不保存配置
                print(f"Failed to serialize config: {e}")
        
        URLStateManager.set_query_params(**params)
    
    @staticmethod
    def update_url_for_designer_task(task_id: str, work_dir: str = None, components=None, constraints=None, config=None, task_type: str = 'designer', backend: str = None):
        """为设计任务更新URL参数"""
        import json
        params = {
            'task_id': task_id,
            'task_type': task_type  # 支持不同类型的设计任务
        }
        if work_dir:
            params['work_dir'] = work_dir
        
        # 如果提供了配置信息，将其序列化存储
        if components or constraints or config:
            try:
                designer_config = {
                    'components': components or [],
                    'constraints': constraints or [],
                    'config': config or {}
                }
                
                # 简化组件信息，只保存关键数据
                if components:
                    simplified_components = []
                    for comp in components:
                        if comp.get('sequence', '').strip():  # 只保存有序列的组件
                            simplified_comp = {
                                'id': comp.get('id'),
                                'type': comp.get('type', 'protein'),
                                'sequence': comp.get('sequence', ''),
                                'num_copies': comp.get('num_copies', 1),
                                'use_msa': comp.get('use_msa', False)
                            }
                            simplified_components.append(simplified_comp)
                    designer_config['components'] = simplified_components
                
                if backend:
                    designer_config['backend'] = backend
                if designer_config['components'] or designer_config['constraints'] or designer_config['config'] or backend:
                    params['designer_config'] = json.dumps(designer_config)
            except Exception as e:
                # 如果序列化失败，不保存配置
                print(f"Failed to serialize designer config: {e}")
        
        URLStateManager.set_query_params(**params)
    
    @staticmethod
    def update_url_for_affinity_task(task_id: str):
        """为亲和力预测任务更新URL参数"""
        URLStateManager.set_query_params(
            task_id=task_id,
            task_type='affinity'
        )
    
    @staticmethod
    def clear_url_params():
        """清除所有URL参数"""
        for key in list(st.query_params.keys()):
            del st.query_params[key]
        # 重置自动切换标志，允许下次URL状态恢复时重新切换
        if 'last_switched_url' in st.session_state:
            st.session_state.last_switched_url = ''
    
    @staticmethod
    def restore_state_from_url():
        """从URL参数恢复应用状态"""
        import json
        import uuid
        
        query_params = URLStateManager.get_query_params()
        
        task_id = query_params.get('task_id')
        task_type = query_params.get('task_type', 'prediction')
        work_dir = query_params.get('work_dir')
        config_str = query_params.get('config')
        designer_config_str = query_params.get('designer_config')
        
        if not task_id:
            return False
        
        restored = False
        
        try:
            if task_type == 'prediction':
                # 恢复预测任务状态
                if st.session_state.task_id != task_id:
                    st.session_state.task_id = task_id
                    st.session_state.results = None
                    st.session_state.error = None
                    st.session_state.raw_zip = None
                    restored = True
                    
                    # 恢复配置信息
                    if config_str:
                        try:
                            config_data = json.loads(config_str)
                            backend = config_data.get('backend')
                            components = config_data.get('components', [])
                            constraints = config_data.get('constraints', [])
                            properties = config_data.get('properties', {})
                            backend = config_data.get('backend')
                            
                            # 恢复组件配置
                            if components:
                                restored_components = []
                                for comp in components:
                                    restored_comp = {
                                        'id': str(uuid.uuid4()),  # 生成新的ID
                                        'type': comp.get('type', 'protein'),
                                        'num_copies': comp.get('num_copies', 1),
                                        'sequence': comp.get('sequence', ''),
                                        'input_method': comp.get('input_method', 'smiles'),
                                        'cyclic': comp.get('cyclic', False),
                                        'use_msa': comp.get('use_msa', True)
                                    }
                                    restored_components.append(restored_comp)
                                st.session_state.components = restored_components
                            
                            # 恢复约束配置
                            if constraints:
                                st.session_state.constraints = constraints
                            
                            # 恢复属性配置
                            if properties:
                                st.session_state.properties.update(properties)
                            
                            if backend in ('boltz', 'alphafold3'):
                                st.session_state.prediction_backend = backend
                        
                        except (json.JSONDecodeError, KeyError) as e:
                            print(f"Failed to restore config from URL: {e}")
                            # 配置恢复失败，但任务ID仍然有效
                    
                    st.toast(f"🔗 从URL恢复预测任务: {task_id[:8]}...", icon="🔄")
            
            elif task_type == 'designer':
                # 恢复分子设计任务状态
                if st.session_state.designer_task_id != task_id:
                    st.session_state.designer_task_id = task_id
                    st.session_state.designer_work_dir = work_dir
                    st.session_state.designer_results = None
                    st.session_state.designer_error = None
                    restored = True
                    
                    # 恢复分子设计配置信息
                    if designer_config_str:
                        try:
                            designer_config_data = json.loads(designer_config_str)
                            backend = designer_config_data.get('backend')
                            if backend in ('boltz', 'alphafold3'):
                                st.session_state.designer_backend = backend
                            components = designer_config_data.get('components', [])
                            constraints = designer_config_data.get('constraints', [])
                            config = designer_config_data.get('config', {})
                            
                            # 恢复组件配置
                            if components:
                                restored_components = []
                                for comp in components:
                                    restored_comp = {
                                        'id': comp.get('id', str(uuid.uuid4())),
                                        'type': comp.get('type', 'protein'),
                                        'sequence': comp.get('sequence', ''),
                                        'num_copies': comp.get('num_copies', 1),
                                        'use_msa': comp.get('use_msa', False)
                                    }
                                    restored_components.append(restored_comp)
                                st.session_state.designer_components = restored_components
                            
                            # 恢复约束配置
                            if constraints:
                                st.session_state.designer_constraints = constraints
                            
                            # 恢复其他配置
                            if config:
                                st.session_state.designer_config.update(config)
                        
                        except (json.JSONDecodeError, KeyError) as e:
                            print(f"Failed to restore designer config from URL: {e}")
                            # 配置恢复失败，但任务ID仍然有效
                    
                    st.toast(f"🔗 从URL恢复设计任务: {task_id[:8]}...", icon="🧪")
            
            elif task_type == 'bicyclic_designer':
                # 恢复双环肽设计任务状态
                if st.session_state.bicyclic_task_id != task_id:
                    st.session_state.bicyclic_task_id = task_id
                    st.session_state.bicyclic_work_dir = work_dir
                    st.session_state.bicyclic_results = None
                    st.session_state.bicyclic_error = None
                    restored = True
                    
                    # 恢复双环肽设计配置信息
                    if designer_config_str:
                        try:
                            designer_config_data = json.loads(designer_config_str)
                            backend = designer_config_data.get('backend')
                            if backend in ('boltz', 'alphafold3'):
                                st.session_state.bicyclic_backend = backend
                            components = designer_config_data.get('components', [])
                            constraints = designer_config_data.get('constraints', [])
                            config = designer_config_data.get('config', {})
                            
                            # 恢复组件配置
                            if components:
                                restored_components = []
                                for comp in components:
                                    restored_comp = {
                                        'id': comp.get('id', str(uuid.uuid4())),
                                        'type': comp.get('type', 'protein'),
                                        'sequence': comp.get('sequence', ''),
                                        'num_copies': comp.get('num_copies', 1),
                                        'use_msa': comp.get('use_msa', False)
                                    }
                                    restored_components.append(restored_comp)
                                st.session_state.bicyclic_components = restored_components
                            
                            # 恢复约束配置
                            if constraints:
                                st.session_state.bicyclic_constraints = constraints
                                print(f"Restored {len(constraints)} bicyclic constraints from URL")
                            else:
                                print("No bicyclic constraints to restore from URL")
                            
                            # 恢复其他配置
                            if config:
                                st.session_state.bicyclic_config.update(config)
                                print(f"Restored {len(config)} bicyclic config items from URL")
                        
                        except (json.JSONDecodeError, KeyError) as e:
                            print(f"Failed to restore bicyclic designer config from URL: {e}")
                            # 配置恢复失败，但任务ID仍然有效
                    
                    # 显示更详细的恢复信息
                    restore_info = []
                    if 'bicyclic_components' in st.session_state and st.session_state.bicyclic_components:
                        restore_info.append(f"{len(st.session_state.bicyclic_components)}组件")
                    if 'bicyclic_constraints' in st.session_state and st.session_state.bicyclic_constraints:
                        restore_info.append(f"{len(st.session_state.bicyclic_constraints)}约束")
                    if 'bicyclic_config' in st.session_state and st.session_state.bicyclic_config:
                        restore_info.append(f"配置")
                    
                    if restore_info:
                        details = "、".join(restore_info)
                        st.toast(f"🔗 从URL恢复双环肽设计: {task_id[:8]}... (包含{details})", icon="🚲")
                    else:
                        st.toast(f"🔗 从URL恢复双环肽设计任务: {task_id[:8]}...", icon="🚲")
            
            elif task_type == 'affinity':
                # 恢复亲和力预测任务状态
                if st.session_state.affinity_task_id != task_id:
                    st.session_state.affinity_task_id = task_id
                    st.session_state.affinity_results = None
                    st.session_state.affinity_error = None
                    restored = True
                    st.toast(f"🔗 从URL恢复亲和力任务: {task_id[:8]}...", icon="🧬")
                
        except Exception as e:
            st.error(f"从URL恢复状态时出错: {e}")
            return False
        
        return restored
    
    @staticmethod
    def get_shareable_url(base_url: str = None) -> str:
        """获取当前状态的可分享URL"""
        if not base_url:
            # 尝试从streamlit获取当前URL基础部分
            base_url = "http://localhost:8501"  # 默认streamlit URL
        
        query_params = URLStateManager.get_query_params()
        if not query_params:
            return base_url
        
        query_string = urllib.parse.urlencode(query_params)
        return f"{base_url}?{query_string}"
    
    @staticmethod
    def get_current_task_info() -> Optional[Dict[str, str]]:
        """获取当前任务信息"""
        query_params = URLStateManager.get_query_params()
        task_id = query_params.get('task_id')
        task_type = query_params.get('task_type', 'prediction')
        
        if task_id:
            return {
                'task_id': task_id,
                'task_type': task_type,
                'short_id': task_id[:8] + "..." if len(task_id) > 8 else task_id
            }
        return None
