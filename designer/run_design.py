# /Boltz-WebUI/designer/run_design.py

"""
run_design.py

该脚本是高级（糖）肽设计器的命令行界面（CLI）入口点。

功能：
- 解析命令行参数以配置设计任务。
- 设置全局日志记录系统。
- 验证用户输入。
- 初始化API客户端和`Designer`类。
- 启动并管理整个设计流程。
"""

import argparse
import os
import time
import logging
import sys
import numpy as np

# 假设api_client在同一目录下或在PYTHONPATH中
from api_client import BoltzApiClient
from design_logic import Designer

def setup_logging():
    """配置全局日志记录。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        stream=sys.stdout,
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    # 为requests库设置一个较高的日志级别，以避免过多的API调用日志
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def main():
    """主执行函数：解析参数并启动设计任务。"""
    setup_logging()
    logger = logging.getLogger(__name__)

    parser = argparse.ArgumentParser(
        description="使用 Boltz-WebUI API 运行并行的蛋白质或糖肽设计任务。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    
    # --- 输入与目标定义 ---
    input_group = parser.add_argument_group('输入与目标定义')
    input_group.add_argument("--yaml_template", required=True, help="定义受体/骨架的模板YAML文件路径。")
    input_group.add_argument("--binder_chain", required=True, help="要设计的肽链的链ID (例如, 'B')。")
    input_group.add_argument("--binder_length", required=True, type=int, help="要设计的肽链的长度。")
    input_group.add_argument("--initial_binder_sequence", type=str, default=None, help="可选的初始肽链序列。如果提供，将以此为起点生成第一代，而不是完全随机。")

    # --- 演化算法控制 ---
    run_group = parser.add_argument_group('演化算法控制')
    run_group.add_argument("--iterations", type=int, default=20, help="设计-评估循环的代数。")
    run_group.add_argument("--population_size", type=int, default=8, help="每一代中并行评估的候选数量。")
    run_group.add_argument("--num_elites", type=int, default=2, help="要保留并用于下一代演化的精英候选数量。必须小于 population_size。")
    run_group.add_argument("--mutation_rate", type=float, default=0.3, help="序列突变率 (0.0-1.0)。控制每一代中发生突变的概率。")
    run_group.add_argument("--weight-iptm", type=float, default=0.7, help="复合评分中 ipTM 分数的权重。")
    run_group.add_argument("--weight-plddt", type=float, default=0.3, help="复合评分中 binder 平均 pLDDT 分数的权重。")

    # --- 糖肽设计 (可选) ---
    glyco_group = parser.add_argument_group('糖肽设计 (可选)')
    glyco_group.add_argument("--glycan_ccd", type=str, default=None, help="通过提供糖基的PDB CCD代码 (例如 'MAN') 来激活糖肽设计模式。")
    glyco_group.add_argument("--glycan_chain", type=str, default='C', help="在生成的YAML文件中分配给糖基配体的链ID。")
    glyco_group.add_argument("--glycosylation_site", type=int, default=None, help="肽链序列上用于共价连接糖基的位置 (1-based索引)。")

    # --- 输出与日志 ---
    output_group = parser.add_argument_group('输出与日志')
    output_group.add_argument("--output_csv", default=f"design_summary_{int(time.time())}.csv", help="输出所有评估设计结果的CSV汇总文件路径。")
    output_group.add_argument("--keep_temp_files", action="store_true", help="如果设置此项，则在运行结束后不删除临时工作目录。")
    
    # --- API 连接 ---
    api_group = parser.add_argument_group('API 连接')
    api_group.add_argument("--server_url", default="http://127.0.0.1:5000", help="Boltz-WebUI 预测 API 服务器的URL。")
    api_group.add_argument("--api_token", help="您的API密钥。也可以通过 'API_SECRET_TOKEN' 环境变量设置。")

    args = parser.parse_args()

    # --- 验证参数 ---
    logger.info("Validating command-line arguments...")
    if args.glycan_ccd and args.glycosylation_site is None:
        parser.error("当指定 --glycan_ccd 时，必须同时提供 --glycosylation_site。")
    
    if args.glycosylation_site is not None:
        if not (1 <= args.glycosylation_site <= args.binder_length):
            parser.error(f"--glycosylation_site 必须是介于 1 和肽链长度 {args.binder_length} 之间的有效位置。")
    
    if args.initial_binder_sequence and len(args.initial_binder_sequence) != args.binder_length:
        parser.error(f"--initial_binder_sequence 的长度 ({len(args.initial_binder_sequence)}) 必须与 --binder_length ({args.binder_length}) 匹配。")

    if not np.isclose(args.weight_iptm + args.weight_plddt, 1.0):
        logger.warning(f"Weights for ipTM ({args.weight_iptm}) and pLDDT ({args.weight_plddt}) do not sum to 1.0. "
                       "This is recommended but not strictly required.")

    api_token = args.api_token or os.environ.get('API_SECRET_TOKEN')
    if not api_token:
        logger.critical("API token is missing. Provide it via --api_token or the 'API_SECRET_TOKEN' environment variable.")
        raise ValueError("必须通过 --api_token 或 'API_SECRET_TOKEN' 环境变量提供API密钥。")
    
    logger.info("Arguments validated successfully.")

    try:
        # 1. 初始化 API 客户端
        logger.info(f"Initializing API client for server: {args.server_url}")
        client = BoltzApiClient(server_url=args.server_url, api_token=api_token)

        # 2. 初始化 Designer
        logger.info(f"Initializing Designer with YAML template: {args.yaml_template}")
        designer = Designer(base_yaml_path=args.yaml_template, client=client)

        # 3. 开始设计任务
        designer.run(
            iterations=args.iterations,
            population_size=args.population_size,
            num_elites=args.num_elites,
            binder_chain_id=args.binder_chain,
            binder_length=args.binder_length,
            initial_binder_sequence=args.initial_binder_sequence,
            mutation_rate=args.mutation_rate,
            glycan_ccd=args.glycan_ccd,
            glycan_chain_id=args.glycan_chain,
            glycosylation_site=args.glycosylation_site - 1 if args.glycosylation_site is not None else None,
            output_csv_path=args.output_csv,
            keep_temp_files=args.keep_temp_files,
            weight_iptm=args.weight_iptm,
            weight_plddt=args.weight_plddt
        )
    except (ValueError, KeyError) as e:
        logger.critical(f"A critical configuration or value error occurred: {e}", exc_info=True)
    except Exception as e:
        logger.critical(f"An unexpected fatal error occurred during the design run: {e}", exc_info=True)


if __name__ == "__main__":
    main()