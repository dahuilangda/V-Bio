# run_single_prediction.py
import sys
import os
import json
import tempfile
import shutil
import traceback

sys.path.append(os.getcwd())
from boltz_wrapper import predict

def find_results_dir(base_dir: str) -> str:
    result_path = None
    max_depth = -1
    for root, dirs, files in os.walk(base_dir):
        if any(f.endswith((".cif")) for f in files):
            depth = root.count(os.sep)
            if depth > max_depth:
                max_depth = depth
                result_path = root

    if result_path:
        print(f"Found results in directory: {result_path}", file=sys.stderr)
        return result_path

    raise FileNotFoundError(f"Could not find any directory containing result files within the base directory {base_dir}")

def main():
    """
    Main function to run a single prediction based on arguments provided in a JSON file.
    The JSON file should contain the necessary parameters for the prediction, including:
    - output_archive_path: Path where the output archive will be saved.
    - yaml_content: YAML content as a string that will be written to a temporary file.
    - Other parameters that will be passed to the predict function as command-line arguments.
    """
    if len(sys.argv) != 2:
        print("Usage: python run_single_prediction.py <args_file_path>")
        sys.exit(1)

    args_file_path = sys.argv[1]

    try:
        with open(args_file_path, 'r') as f:
            predict_args = json.load(f)

        output_archive_path = predict_args.pop("output_archive_path")

        with tempfile.TemporaryDirectory() as temp_dir:
            yaml_content = predict_args.pop("yaml_content")

            tmp_yaml_path = os.path.join(temp_dir, 'data.yaml')
            with open(tmp_yaml_path, 'w') as tmp_yaml:
                tmp_yaml.write(yaml_content)

            predict_args['data'] = tmp_yaml_path
            predict_args['out_dir'] = temp_dir
            
            POSITIONAL_KEYS = ['data']
            
            cmd_positional = []
            cmd_options = []

            for key, value in predict_args.items():
                if key in POSITIONAL_KEYS:
                    cmd_positional.append(str(value))
                else:
                    if value is None:
                        continue
                    if isinstance(value, bool):
                        if value:
                            cmd_options.append(f'--{key}')
                    else:
                        cmd_options.append(f'--{key}')
                        cmd_options.append(str(value))

            cmd_args = cmd_positional + cmd_options

            print(f"DEBUG: Invoking predict with args: {cmd_args}", file=sys.stderr)
            predict.main(args=cmd_args, standalone_mode=False)

            output_directory_path = find_results_dir(temp_dir)

            if not os.listdir(output_directory_path):
                raise NotADirectoryError(f"Prediction result directory was found but is empty: {output_directory_path}")

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