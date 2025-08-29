import os
import json

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
            # In a real scenario, you would send a signal to the process
            # or use a more robust inter-process communication mechanism.
            # For now, we'll simulate success.
            self._update_status_file("stopped")
            self.current_process_id = None
            self.current_status_file = None
            return True
        print("DesignManager: No active design process to stop.")
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
