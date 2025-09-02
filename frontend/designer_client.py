import streamlit as st
import uuid
import os
import yaml
import subprocess
import string
import time
import json
import tempfile
import random
import re
from datetime import datetime
import pandas as pd

try:
    import psutil
except ImportError:
    psutil = None

from frontend.utils import has_cached_msa, get_msa_cache_path

def create_designer_template_yaml(target_protein_sequence: str, target_chain_id: str = "A") -> str:
    """创建 Designer 的模板 YAML 配置"""
    template_dict = {
        'version': 1,
        'sequences': [
            {
                'protein': {
                    'id': target_chain_id,
                    'sequence': target_protein_sequence,
                    'msa': 'empty'
                }
            }
        ]
    }
    return yaml.dump(template_dict, sort_keys=False, indent=2, default_flow_style=False)

def create_designer_complex_yaml(components: list, use_msa: bool = False, constraints: list = None) -> str:
    """为多组分复合物创建 Designer 的模板 YAML 配置"""
    sequences_list = []
    chain_counter = 0
    
    protein_components = [comp for comp in components if comp['type'] == 'protein' and comp.get('sequence', '').strip()]
    
    msa_strategy = "none"
    if use_msa and protein_components:
        cached_count = sum(1 for comp in protein_components if comp.get('use_msa', True) and has_cached_msa(comp['sequence']))
        enabled_count = sum(1 for comp in protein_components if comp.get('use_msa', True))
        
        if enabled_count == 0:
            msa_strategy = "none"
        elif cached_count > 0:
            msa_strategy = "cached"
        else:
            msa_strategy = "auto"
    
    for comp in components:
        if not comp.get('sequence', '').strip():
            continue
            
        num_copies = comp.get('num_copies', 1)
        
        for copy_idx in range(num_copies):
            chain_id = string.ascii_uppercase[chain_counter] if chain_counter < 26 else f"Z{chain_counter-25}"
            chain_counter += 1
            
            if comp['type'] == 'protein':
                protein_dict = {
                    'id': chain_id,
                    'sequence': comp['sequence']
                }
                
                if use_msa:
                    comp_use_msa = comp.get('use_msa', True)
                    
                    if not comp_use_msa:
                        protein_dict['msa'] = 'empty'
                    else:
                        sequence = comp['sequence']
                        
                        if msa_strategy == "cached":
                            enabled_proteins_with_msa = [p for p in protein_components if p.get('use_msa', True)]
                            all_enabled_have_cache = all(
                                has_cached_msa(p['sequence']) for p in enabled_proteins_with_msa
                            ) if enabled_proteins_with_msa else True
                            
                            if all_enabled_have_cache and has_cached_msa(sequence):
                                protein_dict['msa'] = get_msa_cache_path(sequence)
                            else:
                                pass
                        elif msa_strategy == "auto":
                            pass
                        else:
                            protein_dict['msa'] = 'empty'
                else:
                    protein_dict['msa'] = 'empty'
                
                component_dict = {'protein': protein_dict}
            elif comp['type'] == 'dna':
                component_dict = {
                    'dna': {
                        'id': chain_id,
                        'sequence': comp['sequence']
                    }
                }
            elif comp['type'] == 'rna':
                component_dict = {
                    'rna': {
                        'id': chain_id,
                        'sequence': comp['sequence']
                    }
                }
            elif comp['type'] == 'ligand':
                input_method = comp.get('input_method', 'smiles')
                actual_method = 'smiles' if input_method == 'ketcher' else input_method
                component_dict = {
                    'ligand': {
                        'id': chain_id,
                        actual_method: comp['sequence']
                    }
                }
            else:
                continue
                
            sequences_list.append(component_dict)
    
    if not sequences_list:
        raise ValueError("没有有效的组分序列")
        
    template_dict = {'version': 1, 'sequences': sequences_list}
    
    if constraints:
        constraints_list = []
        
        for constraint in constraints:
            constraint_type = constraint.get('type', 'contact')
            
            if constraint_type == 'contact':
                token1 = [constraint['token1_chain'], constraint['token1_residue']]
                token2 = [constraint['token2_chain'], constraint['token2_residue']]
                
                constraint_dict = {
                    'contact': {
                        'token1': token1,
                        'token2': token2,
                        'max_distance': constraint['max_distance'],
                        'force': constraint.get('force', False)
                    }
                }
                
            elif constraint_type == 'bond':
                atom1 = [constraint['atom1_chain'], constraint['atom1_residue'], constraint['atom1_atom']]
                atom2 = [constraint['atom2_chain'], constraint['atom2_residue'], constraint['atom2_atom']]
                
                constraint_dict = {
                    'bond': {
                        'atom1': atom1,
                        'atom2': atom2
                    }
                }
            
            elif constraint_type == 'pocket':
                contacts = constraint.get('contacts', [])
                
                constraint_dict = {
                    'pocket': {
                        'binder': constraint['binder'],
                        'contacts': contacts,
                        'max_distance': constraint['max_distance'],
                        'force': constraint.get('force', False)
                    }
                }
            
            else:
                continue
                
            constraints_list.append(constraint_dict)
        
        if constraints_list:
            template_dict['constraints'] = constraints_list
    
    return yaml.dump(template_dict, sort_keys=False, indent=2, default_flow_style=False)

def run_designer_workflow(params: dict, work_dir: str) -> str:
    """运行 Designer 工作流程"""
    try:
        os.makedirs(work_dir, exist_ok=True)
        
        # 使用模块文件的路径来构建designer脚本的绝对路径
        current_file_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_file_dir)  # 上一级目录
        designer_script_path = os.path.join(project_root, 'designer', 'run_design.py')
        
        if os.path.exists(designer_script_path):
            target_chain_id = params.get('target_chain_id', 'A')
            available_chains = string.ascii_uppercase
            used_chains = set()
            
            try:
                with open(params.get('template_path', ''), 'r') as f:
                    template_data = yaml.safe_load(f)
                    if 'sequences' in template_data:
                        for seq in template_data['sequences']:
                            for seq_type, seq_data in seq.items():
                                if 'id' in seq_data:
                                    used_chains.add(seq_data['id'])
            except Exception as e:
                print(f"Warning: Could not parse template YAML: {e}")
            
            binder_chain_id = None
            for chain in available_chains:
                if chain not in used_chains:
                    binder_chain_id = chain
                    break
            
            if not binder_chain_id:
                binder_chain_id = "Z"
            
            cmd = [
                "python", "run_design.py",
                "--yaml_template", params.get('template_path', ''),
                "--binder_chain", binder_chain_id,
                "--binder_length", str(params.get('binder_length', 20)),
                "--iterations", str(params.get('generations', 5)),
                "--population_size", str(params.get('population_size', 10)),
                "--num_elites", str(params.get('elite_size', 3)),
                "--mutation_rate", str(params.get('mutation_rate', 0.3)),
                "--output_csv", os.path.join(work_dir, f"design_summary_{params.get('task_id', 'unknown')}.csv"),
                "--keep_temp_files"
            ]
            
            # 添加用户约束文件路径（如果存在）
            if params.get('constraints_path'):
                cmd.extend(["--user_constraints", params.get('constraints_path')])
            
            if params.get('enable_enhanced', True):
                cmd.extend([
                    "--convergence-window", str(params.get('convergence_window', 5)),
                    "--convergence-threshold", str(params.get('convergence_threshold', 0.001)),
                    "--max-stagnation", str(params.get('max_stagnation', 3)),
                    "--initial-temperature", str(params.get('initial_temperature', 1.0)),
                    "--min-temperature", str(params.get('min_temperature', 0.1))
                ])
            else:
                cmd.append("--disable-enhanced")
            
            if params.get('design_type') == 'glycopeptide' and params.get('glycan_type'):
                cmd.extend([
                    "--glycan_modification", params.get('glycan_type'),
                    "--modification_site", str(params.get('glycosylation_site', 10))
                ])
            
            if params.get('use_initial_sequence') and params.get('initial_sequence'):
                initial_seq = params.get('initial_sequence', '').upper()
                target_length = params.get('binder_length', 20)
                
                if len(initial_seq) < target_length:
                    amino_acids = "ACDEFGHIKLMNPQRSTVWY"
                    padding = ''.join(random.choices(amino_acids, k=target_length - len(initial_seq)))
                    initial_seq = initial_seq + padding
                elif len(initial_seq) > target_length:
                    initial_seq = initial_seq[:target_length]
                
                cmd.extend([
                    "--initial_binder_sequence", initial_seq
                ])

            if params.get('sequence_mask'):
                cmd.extend(["--sequence_mask", params['sequence_mask']])
            
            server_url = params.get('server_url', 'http://127.0.0.1:5000')
            cmd.extend(["--server_url", server_url])
            
            api_token = os.environ.get('API_SECRET_TOKEN')
            if api_token:
                cmd.extend(["--api_token", api_token])
            
            if not params.get('use_msa', True):
                cmd.append("--no_msa_server")
            
            status_file = os.path.join(work_dir, 'status.json')
            initial_status_data = {
                'task_id': params.get('task_id', 'unknown'),
                'status': 'starting',
                'start_time': datetime.now().isoformat(),
                'params': params,
                'process_id': None
            }
            
            with open(status_file, 'w') as f:
                json.dump(initial_status_data, f, indent=2)
            
            log_file = os.path.join(work_dir, 'design.log')
            
            try:
                with open(log_file, 'w') as log:
                    log.write(f"设计任务开始: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    log.write(f"参数: {json.dumps(params, indent=2)}\n")
                    log.write(f"命令: {' '.join(cmd)}\n")
                    log.write("-" * 50 + "\n")
                    log.flush()
                    
                    env = os.environ.copy()
                    # 使用project_root来设置PYTHONPATH和工作目录
                    designer_dir = os.path.join(project_root, "designer")
                    env['PYTHONPATH'] = designer_dir + ":" + env.get('PYTHONPATH', '')
                    
                    process = subprocess.Popen(
                        cmd,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        cwd=designer_dir,
                        env=env
                    )
                    
                    updated_status_data = {
                        'task_id': params.get('task_id', 'unknown'),
                        'status': 'running',
                        'process_id': process.pid,
                        'start_time': datetime.now().isoformat(),
                        'params': params
                    }
                    
                    with open(status_file, 'w') as f:
                        json.dump(updated_status_data, f, indent=2)
                    
                    return "running"
                    
            except Exception as process_error:
                error_status_data = {
                    'task_id': params.get('task_id', 'unknown'),
                    'status': 'failed',
                    'start_time': initial_status_data['start_time'],
                    'end_time': datetime.now().isoformat(),
                    'params': params,
                    'error': f"进程启动失败: {str(process_error)}"
                }
                
                with open(status_file, 'w') as f:
                    json.dump(error_status_data, f, indent=2)
                
                with open(log_file, 'a') as log:
                    log.write(f"\n❌ 进程启动失败: {str(process_error)}\n")
                
                return "failed"
        else:
            print(f"❌ Designer 脚本未找到: {designer_script_path}")
            
            status_file = os.path.join(work_dir, 'status.json')
            status_data = {
                'task_id': params.get('task_id', 'unknown'),
                'status': 'failed',
                'start_time': datetime.now().isoformat(),
                'end_time': datetime.now().isoformat(),
                'params': params,
                'error': f"Designer script not found at {designer_script_path}"
            }
            
            with open(status_file, 'w') as f:
                json.dump(status_data, f, indent=2)
            
            return "failed"
            
    except Exception as e:
        print(f"Error in run_designer_workflow: {e}")
        
        try:
            status_file = os.path.join(work_dir, 'status.json')
            status_data = {
                'task_id': params.get('task_id', 'unknown'),
                'status': 'failed',
                'start_time': datetime.now().isoformat(),
                'end_time': datetime.now().isoformat(),
                'params': params,
                'error': f"Workflow execution error: {str(e)}"
            }
            
            with open(status_file, 'w') as f:
                json.dump(status_data, f, indent=2)
        except Exception as status_error:
            print(f"Failed to create error status file: {status_error}")
        
        return "failed"

def submit_designer_job(
    template_yaml_content: str,
    design_type: str,
    binder_length: int,
    target_chain_id: str = "A",
    generations: int = 5,
    population_size: int = 10,
    elite_size: int = 3,
    mutation_rate: float = 0.3,
    glycan_type: str = None,
    glycosylation_site: int = None,
    convergence_window: int = 5,
    convergence_threshold: float = 0.001,
    max_stagnation: int = 3,
    initial_temperature: float = 1.0,
    min_temperature: float = 0.1,
    enable_enhanced: bool = True,
    use_initial_sequence: bool = False,
    initial_sequence: str = None,
    sequence_mask: str = None,
    cyclic_binder: bool = False,
    include_cysteine: bool = True,
    use_msa: bool = False,
    user_constraints: list = None  # 新增：用户约束
) -> dict:
    """提交 Designer 任务"""
    try:
        if use_msa:
            try:
                template_data = yaml.safe_load(template_yaml_content)
                target_protein_sequences = []
                
                if 'sequences' in template_data:
                    for seq_item in template_data['sequences']:
                        if 'protein' in seq_item:
                            protein_data = seq_item['protein']
                            sequence = protein_data.get('sequence', '').strip()
                            msa_setting = protein_data.get('msa', 'auto')
                            
                            if sequence and msa_setting != 'empty':
                                target_protein_sequences.append(sequence)
                
                if target_protein_sequences:
                    cached_count = sum(1 for seq in target_protein_sequences if has_cached_msa(seq))
                    if cached_count > 0:
                        st.info(f"✅ 发现 {cached_count}/{len(target_protein_sequences)} 个目标蛋白质已有MSA缓存，将加速设计过程", icon="⚡")
                    else:
                        st.info(f"ℹ️ 检测到 {len(target_protein_sequences)} 个目标蛋白质需要MSA，Boltz将在设计过程中自动生成", icon="🧬")
                else:
                    st.info("ℹ️ 模板中无需MSA的目标蛋白质", icon="💡")
                    
            except Exception as e:
                st.warning(f"⚠️ 模板解析过程中出现错误: {e}，设计将继续进行", icon="⚠️")
        
        work_dir = tempfile.mkdtemp(prefix="boltz_designer_")
        template_path = os.path.join(work_dir, "template.yaml")
        
        with open(template_path, 'w') as f:
            f.write(template_yaml_content)
        
        # 保存用户约束到单独的文件
        constraints_path = os.path.join(work_dir, "user_constraints.json")
        if user_constraints:
            with open(constraints_path, 'w') as f:
                json.dump(user_constraints, f, indent=2)
        
        design_params = {
            'template_path': template_path,
            'constraints_path': constraints_path if user_constraints else None,  # 新增：约束文件路径
            'design_type': design_type,
            'binder_length': binder_length,
            'target_chain_id': target_chain_id,
            'generations': generations,
            'population_size': population_size,
            'elite_size': elite_size,
            'mutation_rate': mutation_rate,
            'work_dir': work_dir,
            'convergence_window': convergence_window,
            'convergence_threshold': convergence_threshold,
            'max_stagnation': max_stagnation,
            'initial_temperature': initial_temperature,
            'min_temperature': min_temperature,
            'enable_enhanced': enable_enhanced,
            'use_initial_sequence': use_initial_sequence,
            'initial_sequence': initial_sequence,
            'sequence_mask': sequence_mask,
            'cyclic_binder': cyclic_binder,
            'include_cysteine': include_cysteine,
            'use_msa': use_msa,
            'user_constraints': user_constraints or []  # 新增：用户约束
        }
        
        if design_type == 'glycopeptide' and glycan_type:
            design_params['glycan_type'] = glycan_type
            design_params['glycosylation_site'] = glycosylation_site
        
        task_id = f"designer_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        design_params['task_id'] = task_id
        
        workflow_status = run_designer_workflow(design_params, work_dir)
        
        return {
            'success': True,
            'task_id': task_id,
            'work_dir': work_dir,
            'params': design_params,
            'initial_status': workflow_status
        }
        
    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }

def get_designer_status(task_id: str, work_dir: str = None) -> dict:
    """获取 Designer 任务状态"""
    try:
        if not work_dir:
            import tempfile
            temp_dir = tempfile.gettempdir()
            
            for item in os.listdir(temp_dir):
                if item.startswith('boltz_designer_'):
                    potential_dir = os.path.join(temp_dir, item)
                    status_file = os.path.join(potential_dir, 'status.json')
                    if os.path.exists(status_file):
                        try:
                            with open(status_file, 'r') as f:
                                status_data = json.load(f)
                                if status_data.get('task_id') == task_id:
                                    work_dir = potential_dir
                                    break
                        except:
                            continue
        
        if not work_dir:
            return {
                'task_id': task_id,
                'state': 'NOT_FOUND',
                'error': '未找到任务工作目录'
            }
        
        status_file = os.path.join(work_dir, 'status.json')
        
        if not os.path.exists(status_file):
            work_dir_contents = []
            try:
                work_dir_contents = os.listdir(work_dir)
            except Exception as e:
                work_dir_contents = [f"Error listing directory: {e}"]
            
            log_file = os.path.join(work_dir, 'design.log')
            log_info = "无日志文件"
            if os.path.exists(log_file):
                try:
                    with open(log_file, 'r') as f:
                        log_content = f.read()
                        log_lines = log_content.strip().split('\n')
                        if log_lines:
                            log_info = f"日志最后几行: {' | '.join(log_lines[-3:])}"
                except Exception as e:
                    log_info = f"读取日志失败: {e}"
            
            return {
                'task_id': task_id,
                'state': 'FAILED',
                'error': f'状态文件不存在。工作目录: {work_dir}, 目录内容: {work_dir_contents}, {log_info}'
            }
        
        with open(status_file, 'r') as f:
            status_data = json.load(f)
        
        current_status = status_data.get('status', 'unknown')
        
        process_still_running = False
        
        if current_status == 'running':
            if 'process_id' in status_data:
                try:
                    if psutil and psutil.pid_exists(status_data['process_id']):
                        proc = psutil.Process(status_data['process_id'])
                        cmdline = proc.cmdline()
                        if cmdline and 'run_design.py' in ' '.join(cmdline):
                            process_still_running = True
                        else:
                            process_still_running = False
                except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
                    process_still_running = False
            
            if not process_still_running:
                error_detected = False
                error_message = ""
                try:
                    log_file = os.path.join(work_dir, 'design.log')
                    if os.path.exists(log_file):
                        with open(log_file, 'r') as f:
                            log_content = f.read()
                            error_indicators = [
                                'error: unrecognized arguments',
                                'error:',
                                'Error:',
                                'ERROR:',
                                'Traceback',
                                'usage:',
                                'FileNotFoundError',
                                'ModuleNotFoundError',
                                'ConnectionError'
                            ]
                            for indicator in error_indicators:
                                if indicator in log_content:
                                    error_detected = True
                                    lines = log_content.split('\n')
                                    for i, line in enumerate(lines):
                                        if indicator in line:
                                            error_lines = lines[i:i+3]
                                            error_message = '\n'.join(error_lines).strip()
                                            break
                                    break
                except Exception:
                    pass
                
                if error_detected:
                    status_data['status'] = 'failed'
                    status_data['end_time'] = datetime.now().isoformat()
                    status_data['error'] = error_message
                    
                    with open(status_file, 'w') as f:
                        json.dump(status_data, f, indent=2)
                    
                    current_status = 'failed'
                else:
                    csv_files = []
                try:
                    for filename in os.listdir(work_dir):
                        if filename.startswith('design_summary_') and filename.endswith('.csv'):
                            csv_path = os.path.join(work_dir, filename)
                            if os.path.exists(csv_path) and os.path.getsize(csv_path) > 0:
                                csv_files.append(csv_path)
                except Exception:
                    pass
                
                log_completed = False
                try:
                    log_file = os.path.join(work_dir, 'design.log')
                    if os.path.exists(log_file):
                        with open(log_file, 'r') as f:
                            log_content = f.read()
                            if any(indicator in log_content for indicator in [
                                'Design Run Finished',
                                '设计任务完成',
                                'Successfully created results package',
                                'Summary CSV successfully saved'
                            ]):
                                log_completed = True
                except Exception:
                    pass
                
                progress_completed = False
                try:
                    log_file = os.path.join(work_dir, 'design.log')
                    if os.path.exists(log_file):
                        progress_info = parse_design_progress(log_file, status_data.get('params', {}))
                        current_gen = progress_info.get('current_generation', 0)
                        total_gen = progress_info.get('total_generations', 1)
                        best_sequences = progress_info.get('current_best_sequences', [])
                        
                        if current_gen > total_gen and total_gen > 0 and best_sequences:
                            progress_completed = True
                        elif current_gen == total_gen and total_gen > 0 and best_sequences:
                            try:
                                log_file = os.path.join(work_dir, 'design.log')
                                if os.path.exists(log_file):
                                    with open(log_file, 'r') as f:
                                        log_content = f.read()
                                        if any(indicator in log_content for indicator in [
                                            'Design Run Finished',
                                            '设计任务完成',
                                            'Successfully created results package',
                                            'All generations completed',
                                            f'Finished all {total_gen} generations'
                                        ]):
                                            progress_completed = True
                                        elif csv_files:
                                            for csv_file in csv_files:
                                                if os.path.exists(csv_file):
                                                    file_age = time.time() - os.path.getmtime(csv_file)
                                                    if file_age < 10:
                                                        try:
                                                            df = pd.read_csv(csv_file)
                                                            if len(df) >= total_gen:
                                                                progress_completed = True
                                                                break
                                                        except:
                                                            pass
                            except Exception:
                                pass
                except Exception:
                    pass
                
                if log_completed or progress_completed:
                    status_data['status'] = 'completed'
                    status_data['end_time'] = datetime.now().isoformat()
                    if csv_files:
                        status_data['csv_files'] = csv_files
                    
                    with open(status_file, 'w') as f:
                        json.dump(status_data, f, indent=2)
                    
                    current_status = 'completed'
        
        result = {
            'task_id': task_id,
            'state': current_status.upper(),
            'start_time': status_data.get('start_time'),
            'work_dir': work_dir
        }
        
        if current_status == 'running':
            log_file = os.path.join(work_dir, 'design.log')
            if os.path.exists(log_file):
                result['progress'] = parse_design_progress(log_file, status_data.get('params', {}))
            else:
                result['progress'] = {
                    'current_generation': 1,
                    'total_generations': status_data.get('params', {}).get('generations', 5),
                    'estimated_progress': 0.1,
                    'best_score': 0.0,
                    'status_message': '任务正在启动...',
                    'pending_tasks': 0,
                    'completed_tasks': 0,
                    'current_status': 'initializing'
                }
        elif current_status == 'completed':
            log_file = os.path.join(work_dir, 'design.log')
            if os.path.exists(log_file):
                final_progress = parse_design_progress(log_file, status_data.get('params', {}))
                result['progress'] = final_progress
                result['progress']['estimated_progress'] = 1.0
                result['progress']['status_message'] = '设计任务已完成'
        elif current_status == 'failed':
            result['error'] = status_data.get('error', '设计任务失败')
        
        if current_status == 'completed' and 'results_summary' in status_data:
            result['results_summary'] = status_data['results_summary']
        
        return result
        
    except Exception as e:
        return {
            'task_id': task_id,
            'state': 'ERROR',
            'error': str(e)
        }

def parse_design_progress(log_file: str, params: dict) -> dict:
    """从日志文件解析设计进度，并从CSV文件读取最佳序列"""
    try:
        with open(log_file, 'r') as f:
            content = f.read()
        
        lines = content.split('\n')
        current_generation = 0
        total_generations = params.get('generations', 5)
        best_score = 0.0
        current_status = "initializing"
        
        pending_task_ids = set()
        completed_task_ids = set()
        current_best_sequences = []
        
        for line in lines:
            line = line.strip()
            
            # 检查任务提交和状态
            if 'Polling task' in line:
                task_id_match = re.search(r'task[_]*([a-f0-9\-]+)', line, re.IGNORECASE)
                task_id = task_id_match.group(1) if task_id_match else None
                
                if 'PENDING' in line and task_id:
                    pending_task_ids.add(task_id)
                    current_status = "waiting_for_prediction"
                elif 'SUCCESS' in line and task_id:
                    completed_task_ids.add(task_id)
                    pending_task_ids.discard(task_id)
                    current_status = "processing_results"
            
            # 检查演化过程的多种日志格式
            elif any(keyword in line for keyword in ['Generation', 'generation', '代演化', '开始第', '完成第', '代数', '第', '代']):
                # 匹配各种可能的代数格式
                generation_patterns = [
                    r'第\s*(\d+)/(\d+)\s*代演化',  # 优先匹配完整格式 "第X/Y代演化"
                    r'(?:Generation|第)\s*(\d+)',
                    r'(?:开始第|完成第)\s*(\d+)\s*代',
                    r'generation\s*(\d+)',
                    r'Gen\s*(\d+)',
                    r'代数[:\s]*(\d+)',
                    r'第(\d+)代',
                    r'(\d+)代演化'
                ]
                
                generation_found = False
                for pattern in generation_patterns:
                    gen_matches = re.findall(pattern, line, re.IGNORECASE)
                    if gen_matches:
                        if isinstance(gen_matches[0], tuple):
                            # 处理 "第X/Y代演化" 格式
                            current_generation = max(current_generation, int(gen_matches[0][0]))
                            if len(gen_matches[0]) > 1:
                                total_generations = int(gen_matches[0][1])
                        else:
                            current_generation = max(current_generation, int(gen_matches[-1]))
                        current_status = "evolving"
                        generation_found = True
                        break
                
                # 如果没有匹配到具体数字，但包含演化相关关键词，说明至少在运行中
                if not generation_found and current_generation == 0:
                    if any(keyword in line for keyword in ['演化', '代', 'generation', 'Gen']):
                        current_status = "evolving"
            
            # 检查任务初始化状态
            elif any(keyword in line.lower() for keyword in ['starting design', '开始设计', '初始化', 'initializing']):
                if current_generation == 0:
                    current_status = "initializing"
            
            # 检查设计完成状态
            elif any(keyword in line.lower() for keyword in ['design completed', '设计完成', 'finished', 'all done']):
                current_status = "completed"
            
            # 检查评分信息
            if any(keyword in line.lower() for keyword in ['best score', '最佳评分', 'best:', 'top score', 'highest score']):
                score_matches = re.findall(r'(\d+\.?\d*(?:[eE][+-]?\d+)?)', line)
                if score_matches:
                    try:
                        candidate_score = float(score_matches[-1])
                        if 0 <= candidate_score <= 10:
                            best_score = max(best_score, candidate_score)
                    except ValueError:
                        pass
                        
            score_patterns = [
                r'score[:\s]+(\d+\.?\d*)',
                r'评分[:\s]+(\d+\.?\d*)',
                r'fitness[:\s]+(\d+\.?\d*)',
                r'ipTM[:\s]+(\d+\.?\d*)',
                r'pLDDT[:\s]+(\d+\.?\d*)'
            ]
            
            for pattern in score_patterns:
                matches = re.findall(pattern, line, re.IGNORECASE)
                if matches:
                    try:
                        candidate_score = float(matches[-1])
                        if 'plddt' in pattern.lower() and candidate_score > 1:
                            candidate_score = candidate_score / 100.0
                        if 0 <= candidate_score <= 1:
                            best_score = max(best_score, candidate_score)
                    except ValueError:
                        pass
        
        work_dir = os.path.dirname(log_file)
        csv_file_path = None
        csv_debug_info = {'work_dir': work_dir, 'files_found': [], 'error': None}
        
        try:
            files_in_dir = os.listdir(work_dir)
            csv_debug_info['files_found'] = [f for f in files_in_dir if f.endswith('.csv')]
            
            for filename in files_in_dir:
                if filename.startswith('design_summary_') and filename.endswith('.csv'):
                    csv_file_path = os.path.join(work_dir, filename)
                    break
        except Exception as e:
            csv_debug_info['error'] = f"List dir error: {str(e)}"
        
        if csv_file_path and os.path.exists(csv_file_path):
            try:
                import pandas as pd
                df = pd.read_csv(csv_file_path)
                csv_debug_info['csv_rows'] = len(df)
                csv_debug_info['csv_columns'] = list(df.columns)
                
                if len(df) > 0 and not df.empty:
                    valid_scores = df['composite_score'].dropna()
                    if len(valid_scores) > 0:
                        csv_best_score = valid_scores.max()
                        if csv_best_score > 0 and csv_best_score <= 1.0:
                            csv_generations = df['generation'].dropna() if 'generation' in df.columns else []
                            if len(csv_generations) > 0 and current_generation > 0:
                                max_csv_generation = int(csv_generations.max())
                                if abs(max_csv_generation - current_generation) <= 1:
                                    best_score = csv_best_score
                            elif current_generation == 0:
                                best_score = csv_best_score
                
                for idx, row in df.head(5).iterrows():
                    sequence = row.get('sequence', '')
                    score = float(row.get('composite_score', '0'))
                    generation = int(row.get('generation', current_generation))
                    iptm = float(row.get('iptm', '0'))
                    plddt = float(row.get('binder_avg_plddt', '0'))
                    
                    if sequence and len(sequence) >= 8:
                        current_best_sequences.append({
                            'sequence': sequence,
                            'score': score,
                            'generation': generation,
                            'iptm': iptm,
                            'plddt': plddt
                        })
                        
            except Exception as e:
                csv_debug_info['read_error'] = str(e)
        else:
            best_score = 0.0
        
        pending_tasks = len(pending_task_ids)
        completed_tasks = len(completed_task_ids)
        
        # 计算进度比例
        if total_generations > 0 and current_generation > 0:
            progress_ratio = min(current_generation / total_generations, 1.0)
        else:
            progress_ratio = 0.05 if current_status != "initializing" else 0.01
        
        # 改进状态消息逻辑
        if current_status == "waiting_for_prediction" and pending_tasks > 0:
            total_prediction_tasks = pending_tasks + completed_tasks
            status_msg = f"等待结构预测完成 ({completed_tasks}/{total_prediction_tasks} 个任务已完成)"
        elif current_status == "evolving":
            if current_generation > 0:
                status_msg = f"第 {current_generation}/{total_generations} 代演化"
            else:
                status_msg = "准备开始演化算法"
        elif current_status == "processing_results":
            status_msg = "处理预测结果中"
        elif current_status == "completed":
            status_msg = "设计任务已完成"
            progress_ratio = 1.0
        elif current_status == "initializing":
            # 检查日志内容，判断是否真的在初始化
            if len(lines) > 10:  # 如果有足够的日志内容
                if any('Starting' in line or '开始' in line or 'Running' in line for line in lines[-10:]):
                    status_msg = "设计任务正在启动..."
                    progress_ratio = 0.05
                else:
                    status_msg = "初始化中..."
                    progress_ratio = 0.01
            else:
                status_msg = "任务启动中..."
                progress_ratio = 0.01
        else:
            # 默认状态 - 分析日志内容
            if pending_tasks > 0 or completed_tasks > 0:
                status_msg = "处理中..."
                progress_ratio = 0.1
            elif current_generation > 0:
                status_msg = f"第 {current_generation}/{total_generations} 代演化"
                progress_ratio = min(current_generation / total_generations, 1.0)
            else:
                status_msg = "任务进行中..."
                progress_ratio = 0.05
        
        return {
            'current_generation': current_generation,
            'total_generations': total_generations,
            'best_score': best_score,
            'estimated_progress': progress_ratio,
            'status_message': status_msg,
            'pending_tasks': pending_tasks,
            'completed_tasks': completed_tasks,
            'current_status': current_status,
            'current_best_sequences': current_best_sequences,
            'debug_info': {
                'sequences_found': len(current_best_sequences),
                'log_lines_processed': len(lines),
                'generation_detected': current_generation > 0,
                'status_detected': current_status,
                'best_score_found': best_score > 0,
                'csv_file_found': csv_file_path is not None,
                'csv_file_path': csv_file_path,
                'csv_debug': csv_debug_info
            }
        }
        
    except Exception as e:
        total_gens = params.get('generations', 5)
        return {
            'current_generation': 0,
            'total_generations': total_gens,
            'best_score': 0.0,
            'estimated_progress': 0.0,
            'status_message': "初始化中",
            'pending_tasks': 0,
            'completed_tasks': 0,
            'current_status': 'initializing',
            'current_best_sequences': [],
            'debug_info': {
                'sequences_found': 0,
                'log_lines_processed': 0,
                'generation_detected': False,
                'status_detected': 'error',
                'error_message': str(e)
            },
            'error': str(e)
        }

def load_designer_results(task_id: str, work_dir: str) -> dict:
    """加载 Designer 结果"""
    try:
        result_files = {
            'summary_csv': None,
            'best_sequences_json': None,
            'evolution_log': None
        }
        
        search_dirs = [
            work_dir,
            os.path.join(work_dir, 'results'),
            '/tmp/boltz_designer',
            './designer/temp_design_*',
            f'./designer/temp_design_run_{task_id.split("_")[-1][:10]}*' if '_' in task_id else None
        ]
        
        search_dirs = [d for d in search_dirs if d is not None]
        
        found_results = []
        
        for search_dir in search_dirs:
            if '*' in search_dir:
                import glob
                matching_dirs = glob.glob(search_dir)
                search_dirs.extend(matching_dirs)
                continue
                
            if not os.path.exists(search_dir):
                continue
                
            try:
                for root, dirs, files in os.walk(search_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        
                        if ('design_summary_' in file or 'design_run_summary' in file) and file.endswith('.csv'):
                            result_files['summary_csv'] = file_path
                            
                        elif 'best_sequences' in file and file.endswith('.json'):
                            result_files['best_sequences_json'] = file_path
                            
                        elif 'evolution' in file or 'log' in file:
                            result_files['evolution_log'] = file_path
                            
            except Exception as e:
                print(f"Error scanning directory {search_dir}: {e}")
                continue
        
        best_sequences = []
        evolution_history = {
            'generations': [],
            'best_scores': [],
            'avg_scores': []
        }
        
        if result_files['summary_csv'] and os.path.exists(result_files['summary_csv']):
            try:
                import pandas as pd
                df = pd.read_csv(result_files['summary_csv'])
                
                for idx, row in df.iterrows():
                    seq_data = {
                        'sequence': row.get('sequence', f'UNKNOWN_SEQ_{idx}'),
                        'score': float(row.get('composite_score', 0.0)) if pd.notna(row.get('composite_score')) else 0.0,
                        'iptm': float(row.get('iptm', 0.0)) if pd.notna(row.get('iptm')) else 0.0,
                        'plddt': float(row.get('binder_avg_plddt', 0.0)) if pd.notna(row.get('binder_avg_plddt')) else 0.0,
                        'generation': int(row.get('generation', 1)) if pd.notna(row.get('generation')) else 1,
                        'results_path': row.get('results_path', '') if pd.notna(row.get('results_path')) else ''
                    }
                    best_sequences.append(seq_data)
                    
                best_sequences.sort(key=lambda x: x['score'], reverse=True)
                
                if len(best_sequences) > 0:
                    generations = sorted(list(set([seq['generation'] for seq in best_sequences])))
                    best_scores = []
                    avg_scores = []
                    
                    for gen in generations:
                        gen_scores = [seq['score'] for seq in best_sequences if seq['generation'] == gen]
                        if gen_scores:
                            best_scores.append(max(gen_scores))
                            avg_scores.append(sum(gen_scores) / len(gen_scores))
                        else:
                            best_scores.append(0.0)
                            avg_scores.append(0.0)
                    
                    evolution_history = {
                        'generations': generations,
                        'best_scores': best_scores,
                        'avg_scores': avg_scores
                    }
                
                print(f"✅ 成功从 {result_files['summary_csv']} 读取了 {len(best_sequences)} 个序列")
                
            except Exception as e:
                print(f"Error reading CSV file {result_files['summary_csv']}: {e}")
        
        if result_files['best_sequences_json'] and os.path.exists(result_files['best_sequences_json']):
            try:
                with open(result_files['best_sequences_json'], 'r') as f:
                    json_data = json.load(f)
                    if 'best_sequences' in json_data:
                        best_sequences = json_data['best_sequences']
                    if 'evolution_history' in json_data:
                        evolution_history = json_data['evolution_history']
                        
                print(f"✅ 成功从 {result_files['best_sequences_json']} 读取了 JSON 数据")
                        
            except Exception as e:
                print(f"Error reading JSON file {result_files['best_sequences_json']}: {e}")
        
        if not best_sequences:
            print(f"❌ 未找到真实设计结果文件。搜索的目录: {search_dirs}")
            print(f"📁 查找的文件类型: CSV汇总文件、JSON结果文件、演化日志")
            raise FileNotFoundError(f"No design results found in searched directories: {search_dirs}")
        
        return {
            'best_sequences': best_sequences,
            'evolution_history': evolution_history,
            'result_files': result_files,
            'search_info': {
                'searched_dirs': search_dirs,
                'found_files': {k: v for k, v in result_files.items() if v is not None}
            }
        }
        
    except Exception as e:
        print(f"Error in load_designer_results: {e}")
        raise Exception(f"Failed to load designer results: {str(e)}")
