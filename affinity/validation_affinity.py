import os
import json
import pandas as pd
import requests
import time
import zipfile
import tempfile
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import re
import math
import csv
from pathlib import Path

def delta_g_to_pIC50(delta_g_kcal_mol):
    """
    将 FEP 的 ΔG (kcal/mol) 转换为 pIC50
    ΔG = -RT * ln(Kd)
    Kd (M) = exp(-ΔG / (RT))
    其中 R = 0.001987 kcal/(mol·K), T = 298.15 K
    RT = 0.5926 kcal/mol
    pIC50 = -log10(Kd) = -log10(exp(-ΔG / RT)) = ΔG / (RT * ln(10)) = ΔG / (0.5926 * 2.303) = ΔG / 1.364
    """
    try:
        dg = float(delta_g_kcal_mol)
        RT = 0.5926  # kcal/mol at 298.15 K
        ln10 = 2.303
        pIC50 = -dg / (RT * ln10)  # 注意 FEP 的 ΔG 通常是负值（结合有利）
        return pIC50
    except (ValueError, TypeError):
        return None

# =========================
# SDF 实验值 -> pIC50
# =========================
def safe_float(x):
    try:
        return float(str(x).strip())
    except:
        return None

def to_pIC50_from_uM(x_uM):
    x = safe_float(x_uM)
    if x is None or x <= 0:
        return None
    return 6.0 - math.log10(x)

def to_M(val, unit_hint):
    v = safe_float(val)
    if v is None or v <= 0:
        return None
    if not unit_hint:
        return None
    u = unit_hint.strip().lower()
    if u == 'um':
        return v * 1e-6
    if u == 'nm':
        return v * 1e-9
    return None

def detect_unit_from_fieldname(field):
    f = field.lower()
    if ('[um]' in f) or ('_um' in f) or (' kd_um' in f) or (' ic50_um' in f) or (' um' in f):
        return 'uM'
    if ('[nm]' in f) or ('_nm' in f) or (' kd_nm' in f) or (' ic50_nm' in f) or (' nm' in f):
        return 'nM'
    return None

def parse_sdf_plain(sdf_path):
    molecules, props, order = [], {}, 0
    field_name = None
    with open(sdf_path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.rstrip('\n')
            if line == '$$$$':
                if props:
                    props['___order'] = order
                    molecules.append(props)
                    order += 1
                props, field_name = {}, None
                continue
            m = re.match(r'^\s*>\s*<([^>]+)>\s*$', line)
            if m:
                field_name = m.group(1).strip()
                props.setdefault(field_name, [])
                continue
            if field_name is not None:
                if line.strip() == '':
                    field_name = None
                else:
                    props[field_name].append(line)
    if props:
        props['___order'] = order
        molecules.append(props)
    for p in molecules:
        for k, v in list(p.items()):
            if k == '___order':
                continue
            if isinstance(v, list):
                p[k] = ' '.join(v).strip()
    return molecules

PREFERRED_NAME_FIELDS = ['Name','name','MOLNAME','MoleculeName','Compound_Name','Title','ID','MolID']
CANDIDATE_EXP_FIELDS = [
    'IC50[uM]','IC50[nM]','IC50[uM](SPA)',
    'IC50 uM', 'IC50 nM',  # 添加对空格的支持
    'Protein/Binding/ITC_Mean_KD_uM_HIF2a_240-350_human;(Num)',
    'KD[uM]','KD[nM]','Ki[uM]','Ki[nM]',
    'IC50','KD','Ki'
]

def pick_name(props):
    for k in PREFERRED_NAME_FIELDS:
        if k in props and str(props[k]).strip():
            return props[k].strip()
    for k in props.keys():
        if k.lower() == 'name' and str(props[k]).strip():
            return props[k].strip()
    return f"mol_{props.get('___order','na')}"

def parse_exp_from_props(props):
    # 优先按候选字段
    for field in CANDIDATE_EXP_FIELDS:
        if field in props and str(props[field]).strip():
            raw = props[field].strip()
            unit = detect_unit_from_fieldname(field)
            m = re.match(r'^\s*([0-9.eE+-]+)\s*([un]M)?\s*$', raw)
            if m:
                val = m.group(1); val_unit = m.group(2)
                if unit is None and val_unit:
                    unit = val_unit
                exp_M = to_M(val, unit)
                if exp_M is not None:
                    return exp_M, field, raw, unit or 'Unknown'
            else:
                val = safe_float(raw)
                if val is not None and val > 0 and unit is not None:
                    exp_M = to_M(val, unit)
                    if exp_M is not None:
                        return exp_M, field, raw, unit or 'Unknown'
    # 兜底：扫描所有含单位的字段
    for k, v in props.items():
        if k == '___order': 
            continue
        unit = detect_unit_from_fieldname(k)
        if unit is None:
            continue
        m = re.match(r'^\s*([0-9.eE+-]+)\s*([un]M)?\s*$', str(v).strip())
        if m:
            val = m.group(1); val_unit = m.group(2)
            if unit is None and val_unit:
                unit = val_unit
            exp_M = to_M(val, unit)
            if exp_M is not None:
                return exp_M, k, str(v).strip(), unit or 'Unknown'
    return None, None, None, None

def load_sdf_pIC50_map(ligands_sdf_path, override_name_field=None):
    """
    返回：
      exp_map: {Name: {'pIC50_exp': float, 'exp_raw': str, 'exp_field': str, 'exp_unit': str}}
    """
    mols = parse_sdf_plain(ligands_sdf_path)
    exp_map = {}
    for props in mols:
        name = (props.get(override_name_field, '').strip() if override_name_field and props.get(override_name_field)
                else pick_name(props))
        exp_M, field_used, raw_str, unit_used = parse_exp_from_props(props)
        pIC50 = (-math.log10(exp_M)) if (exp_M and exp_M > 0) else None
        exp_map[name] = {
            'pIC50_exp': pIC50,
            'exp_raw': raw_str,
            'exp_field': field_used,
            'exp_unit': unit_used
        }
    return exp_map

# =========================
# 你原来的代码（保留/改造）
# =========================
def check_server_status():
    try:
        health_url = "http://127.0.0.1:5000/monitor/health"
        response = requests.get(health_url, timeout=5)
        server_healthy = response.status_code == 200
        monitor_status = None
        try:
            api_token = os.environ.get('API_SECRET_TOKEN')
            headers = {"X-API-Token": api_token} if api_token else {}
            monitor_url = "http://127.0.0.1:5000/monitor/status"
            monitor_response = requests.get(monitor_url, headers=headers, timeout=5)
            if monitor_response.status_code == 200:
                monitor_data = monitor_response.json()
                if monitor_data.get('success') and 'data' in monitor_data:
                    monitor_status = monitor_data['data']
        except:
            pass
        return {'server_healthy': server_healthy,'monitor_status': monitor_status,'timestamp': time.time()}
    except Exception as e:
        return {'server_healthy': False,'error': str(e),'timestamp': time.time()}

def check_task_details(task_id):
    try:
        status_url = f"http://127.0.0.1:5000/status/{task_id}"
        status_response = requests.get(status_url, timeout=10)
        result = {
            'task_id': task_id,
            'status_check_success': status_response.status_code == 200,
            'status_response': status_response.json() if status_response.status_code == 200 else status_response.text,
            'timestamp': time.time()
        }
        try:
            debug_url = f"http://127.0.0.1:5000/debug/{task_id}"
            debug_response = requests.get(debug_url, timeout=5)
            if debug_response.status_code == 200:
                result['debug_info'] = debug_response.json()
        except:
            pass
        return result
    except Exception as e:
        return {'task_id': task_id,'error': str(e),'timestamp': time.time()}

def submit_batch_tasks(pdb_file, sdf_files, target_name, headers, api_url, batch_size=4):
    submitted_tasks = []
    for i in range(0, len(sdf_files), batch_size):
        batch = sdf_files[i:i+batch_size]
        batch_tasks = []
        print(f"\n=== 提交批次 {i//batch_size + 1}/{(len(sdf_files) + batch_size - 1)//batch_size} ===")
        print(f"包含 {len(batch)} 个配体")
        for sdf_file in batch:
            ligand_name = os.path.basename(sdf_file).replace('.sdf', '')
            try:
                with open(pdb_file, 'rb') as pf, open(sdf_file, 'rb') as lf:
                    files = {
                        'protein_file': (os.path.basename(pdb_file), pf.read()),
                        'ligand_file': (os.path.basename(sdf_file), lf.read()),
                    }
                    data = {'ligand_resname': 'LIG','output_prefix': f"{target_name}_{ligand_name}",'priority': 'default'}
                    print(f"  提交任务: {ligand_name}")
                    response = requests.post(api_url, headers=headers, files=files, data=data)
                    if response.status_code in [200, 202]:
                        response_data = response.json()
                        task_id = response_data.get('task_id')
                        if task_id:
                            task_info = {'task_id': task_id,'ligand_name': ligand_name,'sdf_file': sdf_file,'status': 'SUBMITTED','submit_time': time.time()}
                            batch_tasks.append(task_info)
                            print(f"    ✓ 任务已提交: {task_id}")
                        else:
                            print(f"    ✗ 提交失败: 未返回task_id")
                    else:
                        print(f"    ✗ 提交失败: {response.status_code} - {response.text}")
            except Exception as e:
                print(f"    ✗ 提交异常: {e}")
        if batch_tasks:
            submitted_tasks.extend(batch_tasks)
            print(f"  批次提交完成: {len(batch_tasks)}/{len(batch)} 个任务成功提交")
            if i + batch_size < len(sdf_files):
                print(f"  等待当前批次完成后再提交下一批次...")
                wait_for_batch_completion(batch_tasks)
        else:
            print(f"  批次提交失败: 0/{len(batch)} 个任务成功提交")
    return submitted_tasks

def monitor_task_status(task_info):
    task_id = task_info['task_id']
    ligand_name = task_info['ligand_name']
    try:
        status_url = f"http://127.0.0.1:5000/status/{task_id}"
        response = requests.get(status_url, timeout=10)
        if response.status_code == 200:
            status_data = response.json()
            current_state = status_data.get('state', 'UNKNOWN')
            info = status_data.get('info', {})
            task_info['status'] = current_state
            task_info['last_check'] = time.time()
            task_info['raw_response'] = status_data
            if current_state == 'SUCCESS':
                task_info['completed'] = True
                task_info['success'] = True
            elif current_state in ['FAILURE','REVOKED']:
                task_info['completed'] = True
                task_info['success'] = False
                task_info['error_info'] = info
            elif current_state in ['PENDING','STARTED','PROGRESS']:
                task_info['completed'] = False
                if isinstance(info, dict):
                    if 'status' in info:
                        task_info['progress_info'] = info['status']
                        if "Running affinity prediction on GPU" in info['status']:
                            task_info['is_running_on_gpu'] = True
                            gpu_match = info['status'].split("GPU ")
                            if len(gpu_match) > 1:
                                task_info['gpu_id'] = gpu_match[1].strip()
                    if 'progress' in info:
                        task_info['progress_percent'] = info['progress']
                    if 'current_step' in info:
                        task_info['current_step'] = info['current_step']
                else:
                    task_info['progress_info'] = str(info) if info else "任务正在处理中..."
                if current_state == 'PROGRESS':
                    task_info['progress_description'] = "正在GPU上执行亲和力预测" if task_info.get('is_running_on_gpu') else "任务正在进行中"
                elif current_state == 'STARTED':
                    task_info['progress_description'] = "任务已开始执行"
                elif current_state == 'PENDING':
                    submit_time = task_info.get('submit_time', time.time())
                    current_time = time.time()
                    if (current_time - submit_time) > 300:
                        task_info['progress_description'] = f"任务已提交 {int((current_time - submit_time)/60)} 分钟，可能正在队列中"
                    else:
                        task_info['progress_description'] = "任务在队列中等待"
            else:
                task_info['completed'] = False
                task_info['progress_info'] = f"任务状态: {current_state}"
        else:
            task_info['status'] = 'ERROR'
            task_info['error_info'] = f"HTTP {response.status_code} - {response.text[:200]}"
    except Exception as e:
        task_info['status'] = 'ERROR'
        task_info['error_info'] = str(e)
    return task_info

def wait_for_batch_completion(batch_tasks, max_wait_time=7200, check_interval=30):
    print(f"\n--- 监控 {len(batch_tasks)} 个任务的执行状态 ---")
    start_time = time.time()
    completed_tasks = []
    consecutive_no_change_count = 0
    last_completed_count = 0
    task_id_map = {task['task_id']: task['ligand_name'] for task in batch_tasks}
    while time.time() - start_time < max_wait_time:
        active_tasks = [task for task in batch_tasks if not task.get('completed', False)]
        if not active_tasks:
            print("✓ 所有任务已完成")
            break
        current_time = time.strftime('%H:%M:%S')
        elapsed_time = int(time.time() - start_time)
        print(f"\n检查时间: {current_time} (已运行 {elapsed_time//60}:{elapsed_time%60:02d})")
        print(f"剩余任务: {len(active_tasks)}")
        if len(active_tasks) <= 10:
            print(f"活跃任务ID: {[task['task_id'][:8] + '...' for task in active_tasks]}")
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_to_task = {executor.submit(monitor_task_status, task): task for task in active_tasks}
            status_changes = []
            for future in as_completed(future_to_task):
                task = future_to_task[future]
                try:
                    updated_task = future.result()
                    old_status = task.get('status', 'UNKNOWN')
                    new_status = updated_task.get('status', 'UNKNOWN')
                    if old_status != new_status:
                        status_changes.append(f"{task['ligand_name']}: {old_status} -> {new_status}")
                    task.update(updated_task)
                    if task.get('completed'):
                        if task.get('success'):
                            print(f"  ✓ {task['ligand_name']}: 任务完成")
                            completed_tasks.append(task)
                        else:
                            error_info = task.get('error_info', '未知错误')
                            print(f"  ✗ {task['ligand_name']}: 任务失败 - {error_info}")
                            completed_tasks.append(task)
                    else:
                        status = task.get('status', 'UNKNOWN')
                        progress = task.get('progress_info', '')
                        progress_desc = task.get('progress_description', '')
                        gpu_info = f" (GPU {task.get('gpu_id')})" if task.get('gpu_id') else ""
                        if progress_desc:
                            print(f"  ⏳ {task['ligand_name']}: {progress_desc}{gpu_info}")
                        elif progress:
                            if "Running affinity prediction on GPU" in progress:
                                gpu_match = progress.split("GPU ")
                                gpu_num = gpu_match[1].strip() if len(gpu_match) > 1 else "?"
                                print(f"  🔥 {task['ligand_name']}: 正在GPU {gpu_num}上执行亲和力预测")
                            elif "Task is waiting in the queue" in progress:
                                submit_time = task.get('submit_time', time.time())
                                wait_time = int(time.time() - submit_time)
                                if wait_time > 600:
                                    print(f"  ⏰ {task['ligand_name']}: 可能正在执行中 (已等待 {wait_time//60} 分钟)")
                                else:
                                    print(f"  ⏰ {task['ligand_name']}: 队列中等待 ({wait_time//60}:{wait_time%60:02d})")
                            else:
                                print(f"  ⏳ {task['ligand_name']}: {progress}")
                        else:
                            if status == 'PROGRESS':
                                print(f"  🔄 {task['ligand_name']}: 任务执行中...")
                            elif status == 'STARTED':
                                print(f"  🚀 {task['ligand_name']}: 任务已启动")
                            else:
                                print(f"  ⏳ {task['ligand_name']}: {status}")
                        if task.get('raw_response') and len(active_tasks) <= 5:
                            raw_info = task['raw_response'].get('info', {})
                            if isinstance(raw_info, dict) and raw_info:
                                debug_info = {k: v for k, v in raw_info.items() if k not in ['status']}
                                if debug_info:
                                    print(f"      调试信息: {debug_info}")
                except Exception as e:
                    print(f"  ⚠️  {task['ligand_name']}: 状态检查异常 - {e}")
        if status_changes:
            print(f"\n状态变化:")
            for change in status_changes:
                print(f"  📋 {change}")
        completed_count = len([t for t in batch_tasks if t.get('completed', False)])
        success_count = len([t for t in batch_tasks if t.get('completed', False) and t.get('success', False)])
        print(f"进度: {completed_count}/{len(batch_tasks)} 完成 ({success_count} 成功)")
        if completed_count == last_completed_count:
            consecutive_no_change_count += 1
        else:
            consecutive_no_change_count = 0
            last_completed_count = completed_count
        if consecutive_no_change_count >= 5:
            total_wait_time = consecutive_no_change_count * check_interval
            print(f"⚠️  已连续 {total_wait_time//60} 分钟无任务完成，检查系统状态...")
            server_status = check_server_status()
            if not server_status['server_healthy']:
                print(f"❌ 服务器状态异常: {server_status.get('error', '未知错误')}")
            elif server_status.get('monitor_status'):
                print(f"📊 服务器监控状态: {server_status['monitor_status']}")
            print("\n详细任务状态:")
            for task in active_tasks[:3]:
                task_info = task.get('raw_response', {})
                submit_time = task.get('submit_time', time.time())
                wait_time = int(time.time() - submit_time)
                print(f"  任务 {task['ligand_name']} ({task['task_id'][:8]}...):")
                print(f"    状态: {task.get('status', 'UNKNOWN')}")
                print(f"    等待时间: {wait_time//60}:{wait_time%60:02d}")
                print(f"    原始信息: {task_info.get('info', {})}")
            if consecutive_no_change_count >= 10:
                print("\n🔍 进行深度任务检查...")
                for task in active_tasks[:2]:
                    detailed_info = check_task_details(task['task_id'])
                    print(f"  任务 {task['ligand_name']} 详细信息:")
                    print(f"    {detailed_info}")
                print("\n🚨 建议检查:")
                print("  1. API服务器是否正常运行")
                print("  2. Celery worker是否正常工作")
                print("  3. GPU资源是否可用")
                print("  4. 任务队列是否有积压")
                print("  5. 运行 'ps aux | grep celery' 检查worker进程")
                print("  6. 检查 celery.log 和其他日志文件")
        if completed_count < len(batch_tasks):
            print(f"等待 {check_interval} 秒后继续检查...")
            time.sleep(check_interval)
    final_completed = len([t for t in batch_tasks if t.get('completed', False)])
    if final_completed < len(batch_tasks):
        remaining_tasks = len(batch_tasks) - final_completed
        print(f"⚠️  等待超时，仍有 {remaining_tasks} 个任务未完成")
        incomplete_tasks = [t for t in batch_tasks if not t.get('completed', False)]
        print("未完成的任务:")
        for task in incomplete_tasks:
            elapsed = int(time.time() - task.get('submit_time', time.time()))
            print(f"  - {task['ligand_name']} ({task['task_id'][:8]}...): {task.get('status', 'UNKNOWN')} (运行 {elapsed//60}:{elapsed%60:02d})")
    return completed_tasks

def download_and_parse_results(task_info, output_dir):
    """
    返回:
      {
        'task_id','ligand_name',
        'log_ic50': mean_affinity,   # API csv 中 log10(uM)
        'ic50_uM': 10**mean_affinity,
        'pIC50_pred': 6 - mean_affinity,
        ...
      }
    """
    task_id = task_info['task_id']
    ligand_name = task_info['ligand_name']
    try:
        results_url = f"http://127.0.0.1:5000/results/{task_id}"
        response = requests.get(results_url, timeout=30)
        if response.status_code == 200:
            task_output_dir = os.path.join(output_dir, task_id)
            os.makedirs(task_output_dir, exist_ok=True)
            zip_path = os.path.join(task_output_dir, f"{task_id}_results.zip")
            with open(zip_path, 'wb') as f:
                f.write(response.content)
            results = {'task_id': task_id, 'ligand_name': ligand_name}
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(task_output_dir)
                for root, dirs, files in os.walk(task_output_dir):
                    for file in files:
                        if file.endswith('.csv') and 'affinity' in file.lower():
                            csv_path = os.path.join(root, file)
                            try:
                                df = pd.read_csv(csv_path)
                                if not df.empty:
                                    row = df.iloc[0]
                                    affinity_cols = [c for c in df.columns if any(k in c.lower() for k in ['affinity_pred_value','ic50','binding'])]
                                    if affinity_cols:
                                        vals = []
                                        for c in affinity_cols:
                                            if pd.notna(row[c]):
                                                vals.append(float(row[c]))
                                        if vals:
                                            import numpy as np
                                            mean_affinity = float(np.mean(vals))  # 视为 log10(uM)
                                            std_affinity  = float(np.std(vals)) if len(vals) > 1 else 0.0
                                            ic50_uM = math.pow(10, mean_affinity)
                                            pIC50_pred = 6.0 - mean_affinity
                                            results.update({
                                                'log_ic50': mean_affinity,
                                                'ic50_uM': ic50_uM,
                                                'pIC50_pred': pIC50_pred,
                                                'affinity_std': std_affinity,
                                                'affinity_values': vals,
                                                'csv_file': csv_path
                                            })
                                            print(f"    ✓ 解析结果: IC50 = {ic50_uM:.3f} μM (log10(uM) = {mean_affinity:.3f}, pIC50_pred = {pIC50_pred:.3f})")
                                            return results
                                print(f"    可用列: {list(df.columns)}")
                            except Exception as e:
                                print(f"    ✗ CSV解析失败: {e}")
                                continue
            print(f"    ⚠️  未找到有效的亲和力数据")
            return results
        else:
            print(f"    ✗ 下载失败: HTTP {response.status_code}")
            return None
    except Exception as e:
        print(f"    ✗ 下载异常: {e}")
        return None

def split_sdf(sdf_file, output_dir):
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    with open(sdf_file, 'r') as f:
        sdf_content = f.read()
    molecules = sdf_content.split('$$$$')
    created_files = []
    for i, molecule in enumerate(molecules):
        if molecule.strip():
            mol_name = f"ligand_{i+1}"
            lines = molecule.strip().split('\n')
            if len(lines) > 0:
                mol_name = lines[0].strip()
            sanitized_mol_name = mol_name.replace('/', '_')
            output_filename = os.path.join(output_dir, f"{sanitized_mol_name}.sdf")
            with open(output_filename, 'w') as out_f:
                out_f.write(molecule.strip() + '\n$$$$')
            created_files.append(output_filename)
    return created_files

def get_target_directories(data_dir):
    return [os.path.join(data_dir, d) for d in os.listdir(data_dir) if os.path.isdir(os.path.join(data_dir, d)) and d != 'output']

def find_files(target_dir):
    pdb_file = None
    sdf_file = None
    for f in os.listdir(target_dir):
        if f.endswith('.pdb'):
            pdb_file = os.path.join(target_dir, f)
        elif f == 'ligands.sdf':
            sdf_file = os.path.join(target_dir, f)
    return pdb_file, sdf_file

def run_analysis():
    # --- API Configuration ---
    API_URL = "http://127.0.0.1:5000/api/affinity_separate"
    API_TOKEN = os.environ.get('API_SECRET_TOKEN')
    BATCH_SIZE = 4
    # -------------------------
    data_dir = '/data/Boltz-WebUI/affinity/data'
    output_base_dir = os.path.join(data_dir, 'output')
    results_output_dir = os.path.join(output_base_dir, 'api_results')
    if not API_TOKEN:
        print("错误: 请设置 API_SECRET_TOKEN 环境变量")
        return
    print("🔍 检查服务器状态...")
    server_status = check_server_status()
    if server_status['server_healthy']:
        print("✅ 服务器状态正常")
        if server_status.get('monitor_status'):
            monitor_info = server_status['monitor_status']
            gpu_status = monitor_info.get('gpu_status', {})
            running_tasks = monitor_info.get('running_tasks', [])
            print(f"   📊 GPU状态: {gpu_status.get('in_use_count', 0)}/{gpu_status.get('in_use_count', 0) + gpu_status.get('available_count', 0)} 正在使用")
            if running_tasks:
                print(f"   🔥 正在运行 {len(running_tasks)} 个任务:")
                for task in running_tasks[:3]:
                    gpu_id = task.get('gpu_id', '?')
                    running_time = task.get('running_time', 'unknown')
                    print(f"      GPU {gpu_id}: {task.get('task_id', '')[:8]}... (运行时间: {running_time})")
                if len(running_tasks) > 3:
                    print(f"      ... 还有 {len(running_tasks) - 3} 个任务")
            print(f"   ⏰ 检查时间: {time.strftime('%H:%M:%S')}")
    else:
        print(f"❌ 服务器状态异常: {server_status.get('error', '未知错误')}")
        print("请检查API服务是否正常运行")
        return
    print("🔧 运行配置:")
    print(f"   API URL: {API_URL}")
    print(f"   批处理大小: {BATCH_SIZE}")
    print(f"   数据目录: {data_dir}")
    print(f"   输出目录: {results_output_dir}")
    print(f"   API Token: {'已设置' if API_TOKEN else '未设置'}")
    headers = {"X-API-Token": API_TOKEN}
    target_dirs = get_target_directories(data_dir)
    print(f"发现 {len(target_dirs)} 个目标目录")
    print(f"批处理大小: {BATCH_SIZE}")
    print("=" * 60)

    for target_dir in target_dirs:
        target_name = os.path.basename(target_dir)
        print(f"\n🎯 处理目标: {target_name}")
        pdb_file, ligands_sdf = find_files(target_dir)
        if not pdb_file or not ligands_sdf:
            print(f"  ⚠️  跳过 {target_name}: 未找到 PDB 或 ligands.sdf 文件")
            continue

        # 先解析 SDF 得到实验 pIC50 映射
        print("  🧪 解析 ligands.sdf 实验值 ...")
        exp_map = load_sdf_pIC50_map(ligands_sdf)
        n_exp = sum(1 for v in exp_map.values() if v.get('pIC50_exp') is not None)
        print(f"  ✅ 从 SDF 读取到 {n_exp} 个可用的实验 pIC50 / {len(exp_map)}")

        split_sdf_dir = os.path.join(output_base_dir, target_name, 'split_sdfs')
        individual_sdf_files = split_sdf(ligands_sdf, split_sdf_dir)
        print(f"  发现 {len(individual_sdf_files)} 个配体分子")

        # FEP+ 结果变为可选，不中断流程
        fep_results_file = os.path.join(target_dir, 'results_20ns.csv')
        if not os.path.exists(fep_results_file):
            fep_results_file = os.path.join(target_dir, 'results_5ns.csv')
        fep_df = None
        if os.path.exists(fep_results_file):
            try:
                fep_df = pd.read_csv(fep_results_file)
                # 处理配体名称，去掉 ".0" 后缀以匹配 SDF 中的名称
                fep_df['Ligand'] = fep_df['Ligand'].astype(str).str.replace('.0','',regex=False)
                print(f"  （可选）加载 FEP+ 结果: {len(fep_df)} 行")
            except Exception as e:
                print(f"  ⚠️ FEP+ 文件读取失败：{e}")

        # 提交任务
        print(f"\n🚀 开始批量提交任务...")
        all_tasks = submit_batch_tasks(
            pdb_file, individual_sdf_files, target_name,
            headers, API_URL, BATCH_SIZE
        )
        if not all_tasks:
            print(f"  ❌ 没有成功提交任何任务")
            continue

        print(f"\n📊 处理结果并生成 pIC50 比较...")
        comparison_rows = []
        for task in all_tasks:
            if task.get('completed') and task.get('success'):
                print(f"\n  📥 下载结果: {task['ligand_name']}")
                result_data = download_and_parse_results(task, results_output_dir)
                if result_data and ('pIC50_pred' in result_data):
                    ligand_name = task['ligand_name']
                    # 预测
                    pIC50_pred = result_data['pIC50_pred']
                    ic50_uM_pred = result_data.get('ic50_uM')
                    # 实验（来自 SDF）
                    exp_info = exp_map.get(ligand_name, {})
                    pIC50_exp = exp_info.get('pIC50_exp')
                    exp_raw = exp_info.get('exp_raw')
                    exp_field = exp_info.get('exp_field')
                    exp_unit = exp_info.get('exp_unit')

                    if pIC50_exp is None:
                        print(f"      ⚠️  {ligand_name} 在 SDF 中未找到可用实验值，将保留空值")

                    row = {
                        'Target': target_name,
                        'Ligand': ligand_name,
                        'pIC50_pred': pIC50_pred,
                        'IC50_uM_pred': ic50_uM_pred,
                        'pIC50_exp': pIC50_exp,
                        'Exp_raw_in_SDF': exp_raw,
                        'Exp_field_in_SDF': exp_field,
                        'Exp_unit_inferred': exp_unit,
                        'Task_ID': task['task_id']
                    }

                    # 若存在 FEP，可附带并转换为 pIC50
                    if fep_df is not None and 'Ligand' in fep_df.columns:
                        _row = fep_df[fep_df['Ligand'] == ligand_name]
                        if not _row.empty:
                            for c in ['Pred. Binding (ΔG)','Exp. Binding (ΔG)','Pred. ΔG','Exp. ΔG']:
                                if c in _row.columns:
                                    original_dg = _row.iloc[0][c]
                                    row[f'FEP_{c}'] = original_dg
                                    # 转换为 pIC50
                                    if 'Pred' in c:
                                        pIC50_fep = delta_g_to_pIC50(original_dg)
                                        if pIC50_fep is not None:
                                            row['FEP_pIC50_pred'] = pIC50_fep
                                    elif 'Exp' in c:
                                        pIC50_fep = delta_g_to_pIC50(original_dg)
                                        if pIC50_fep is not None:
                                            row['FEP_pIC50_exp'] = pIC50_fep

                    comparison_rows.append(row)
                    # 友好打印
                    if pIC50_exp is not None:
                        print(f"      pIC50_pred = {pIC50_pred:.3f} | pIC50_exp = {pIC50_exp:.3f} | Δ = {pIC50_pred - pIC50_exp:+.3f}")
                    else:
                        print(f"      pIC50_pred = {pIC50_pred:.3f} | pIC50_exp = NA")
                else:
                    print(f"      ❌ 无法解析 {task['ligand_name']} 的预测结果")
            else:
                print(f"  ❌ 任务失败: {task['ligand_name']} - {task.get('error_info', '未知错误')}")

        # 保存对比结果
        if comparison_rows:
            comparison_df = pd.DataFrame(comparison_rows)
            os.makedirs(results_output_dir, exist_ok=True)
            output_csv = os.path.join(results_output_dir, f"{target_name}_pIC50_comparison.csv")
            comparison_df.to_csv(output_csv, index=False)

            print(f"\n📈 {target_name} 处理完成:")
            print(f"    成功预测: {len(comparison_rows)}/{len(individual_sdf_files)} 个配体")
            print(f"    pIC50 对比结果保存至: {output_csv}")

            # 误差统计（仅对同时有实验与预测的行）
            mask = comparison_df['pIC50_exp'].notna() & comparison_df['pIC50_pred'].notna()
            if mask.any():
                diffs = (comparison_df.loc[mask, 'pIC50_pred'] - comparison_df.loc[mask, 'pIC50_exp']).abs()
                mae = diffs.mean()
                std = diffs.std()
                print(f"    |pIC50_pred - pIC50_exp|: MAE = {mae:.3f} ± {std:.3f} (n={mask.sum()})")
            else:
                print("    ⚠️ 无可用于 MAE 的配对样本（缺少实验或预测 pIC50）")
        else:
            print(f"  ❌ {target_name}: 没有成功的预测结果")

        print("=" * 60)

    print("\n🎉 所有目标处理完成！")

if __name__ == '__main__':
    run_analysis()
