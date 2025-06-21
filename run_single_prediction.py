# run_single_prediction.py
import sys
import os
import json
import tempfile
import shutil
from pathlib import Path
import traceback

sys.path.append(os.getcwd())
from boltz_wrapper import run_prediction

def find_results_dir(base_dir: str) -> str:
    for root, dirs, files in os.walk(base_dir):
        if any(f.endswith(".cif") or f.endswith(".pdb") for f in files):
            print(f"Found results in directory: {root}", file=sys.stderr)
            return root
    raise FileNotFoundError(f"Could not find any directory containing .cif or .pdb files within the base directory {base_dir}")

def main():
    """
    独立的预测进程入口。
    """
    if len(sys.argv) != 2:
        print("Usage: python run_single_prediction.py <args_file_path>")
        sys.exit(1)

    args_file_path = sys.argv[1]

    try:
        with open(args_file_path, 'r') as f:
            predict_args = json.load(f)

        # 1. 从参数中获取父进程指定的输出路径
        output_archive_path = predict_args.pop("output_archive_path")

        with tempfile.TemporaryDirectory() as temp_dir:
            yaml_content = predict_args.pop("yaml_content")
            
            # 使用 with 语句确保临时 yaml 文件被自动清理
            tmp_yaml_path = os.path.join(temp_dir, 'data.yaml')
            with open(tmp_yaml_path, 'w') as tmp_yaml:
                tmp_yaml.write(yaml_content)

            predict_args['devices'] = [0]
            predict_args['data'] = tmp_yaml_path
            predict_args['out_dir'] = temp_dir
            predict_args['accelerator'] = 'gpu'
            
            run_prediction(**predict_args)

            output_directory_path = find_results_dir(temp_dir)

            if not os.listdir(output_directory_path):
                raise NotADirectoryError(f"Prediction result directory was found but is empty: {output_directory_path}")

            # 2. 使用父进程提供的路径来创建压缩包
            archive_base_name = output_archive_path.rsplit('.', 1)[0]
            
            created_archive_path = shutil.make_archive(
                base_name=archive_base_name,
                format='zip',
                root_dir=output_directory_path
            )

            if not os.path.exists(created_archive_path):
                raise FileNotFoundError(f"CRITICAL ERROR: Archive not found at {created_archive_path} immediately after creation.")
            
            print(f"DEBUG: Archive successfully created at: {created_archive_path}", file=sys.stderr)

    except Exception as e:
        print(f"Error during prediction subprocess: {e}\n{traceback.format_exc()}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()