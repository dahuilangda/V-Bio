import os
import json
import signal
import psutil
import subprocess

class DesignManager:
    def __init__(self):
        self.current_process_id = None
        self.current_status_file = None

    def set_current_process_info(self, process_id, status_file):
        self.current_process_id = process_id
        self.current_status_file = status_file
        print(f"DesignManager: Set current process info - ID: {process_id}, Status File: {status_file}")

    def stop_current_design(self):
        if self.current_process_id:
            print(f"DesignManager: Attempting to stop process {self.current_process_id}")
            
            try:
                # 首先尝试优雅停止
                if self._graceful_stop_process(self.current_process_id):
                    print(f"DesignManager: Successfully stopped process {self.current_process_id}")
                    self._update_status_file("stopped")
                    self.current_process_id = None
                    self.current_status_file = None
                    return True
                else:
                    print(f"DesignManager: Failed to stop process {self.current_process_id}")
                    return False
                    
            except Exception as e:
                print(f"DesignManager: Error stopping process {self.current_process_id}: {e}")
                # 即使出错也要清理状态
                self._update_status_file("error_stopped")
                self.current_process_id = None
                self.current_status_file = None
                return False
        
        print("DesignManager: No active design process to stop.")
        return False
    
    def _graceful_stop_process(self, process_id):
        """优雅地停止进程"""
        try:
            # 检查进程是否存在
            if not psutil.pid_exists(process_id):
                print(f"DesignManager: Process {process_id} does not exist.")
                return True
            
            process = psutil.Process(process_id)
            
            # 获取进程及其所有子进程
            children = process.children(recursive=True)
            
            # 首先尝试SIGTERM
            print(f"DesignManager: Sending SIGTERM to process {process_id}")
            process.terminate()
            
            # 也向所有子进程发送SIGTERM
            for child in children:
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            
            # 等待进程终止
            try:
                process.wait(timeout=10)  # 等待10秒
                return True
            except psutil.TimeoutExpired:
                print(f"DesignManager: Process {process_id} did not terminate gracefully, forcing kill")
                
                # 如果优雅停止失败，强制杀死进程
                process.kill()
                for child in children:
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                
                # 再等待3秒确认
                try:
                    process.wait(timeout=3)
                    return True
                except psutil.TimeoutExpired:
                    print(f"DesignManager: Failed to kill process {process_id}")
                    return False
                    
        except psutil.NoSuchProcess:
            print(f"DesignManager: Process {process_id} already terminated.")
            return True
        except Exception as e:
            print(f"DesignManager: Error in graceful stop: {e}")
            return False

    def _update_status_file(self, status):
        if self.current_status_file and os.path.exists(self.current_status_file):
            try:
                with open(self.current_status_file, 'r+') as f:
                    data = json.load(f)
                    data['status'] = status
                    f.seek(0)
                    json.dump(data, f, indent=4)
                    f.truncate()
                print(f"DesignManager: Updated status file {self.current_status_file} with status: {status}")
            except Exception as e:
                print(f"DesignManager: Error updating status file {self.current_status_file}: {e}")
        else:
            print(f"DesignManager: Status file {self.current_status_file} not found or not set.")

design_manager = DesignManager()
