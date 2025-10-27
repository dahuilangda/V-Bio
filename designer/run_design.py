# /Boltz-WebUI/designer/run_design.py

import argparse
import os
import time
import logging
import sys
import numpy as np
import json  # 新增：用于处理约束文件

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
        description="使用 Boltz-WebUI API 运行并行的蛋白质、糖肽或双环肽设计任务。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    
    # --- 设计模式选择 ---
    design_mode_group = parser.add_argument_group('设计模式')
    design_mode_group.add_argument("--design_type", type=str, default="linear", choices=["linear", "glycopeptide", "bicyclic"],
                                 help="选择设计类型：'linear'（线性多肽），'glycopeptide'（糖肽），或 'bicyclic'（双环肽）。")

    # --- 输入与目标定义 ---
    input_group = parser.add_argument_group('输入与目标定义')
    input_group.add_argument("--yaml_template", required=True, help="定义受体/骨架的模板YAML文件路径。")
    input_group.add_argument("--binder_chain", required=True, help="要设计的肽链的链ID (例如, 'B')。")
    input_group.add_argument("--binder_length", required=True, type=int, help="要设计的肽链的长度。")
    input_group.add_argument("--initial_binder_sequence", type=str, default=None, help="可选的初始肽链序列。如果提供，将以此为起点生成第一代，而不是完全随机。")
    input_group.add_argument("--sequence_mask", type=str, default=None, help="序列掩码，用于指定固定位置的氨基酸。格式：'X-A-X-L-X'，其中X表示可变位置，字母表示固定氨基酸。长度必须与binder_length匹配。")
    input_group.add_argument("--user_constraints", type=str, default=None, help="用户定义约束的JSON文件路径。约束将应用于生成的结合肽。")  # 新增：约束文件参数

    # --- 演化算法控制 ---
    run_group = parser.add_argument_group('演化算法控制')
    run_group.add_argument("--iterations", type=int, default=20, help="设计-评估循环的代数。")
    run_group.add_argument("--population_size", type=int, default=8, help="每一代中并行评估的候选数量。")
    run_group.add_argument("--num_elites", type=int, default=2, help="要保留并用于下一代演化的精英候选数量。必须小于 population_size。")
    run_group.add_argument("--mutation_rate", type=float, default=0.3, help="序列突变率 (0.0-1.0)。控制每一代中发生突变的概率。")
    run_group.add_argument("--weight-iptm", type=float, default=0.7, help="复合评分中 ipTM 分数的权重。")
    run_group.add_argument("--weight-plddt", type=float, default=0.3, help="复合评分中 binder 平均 pLDDT 分数的权重。")
    
    # --- 增强功能选项 ---
    enhanced_group = parser.add_argument_group('增强功能选项')
    enhanced_group.add_argument("--enable-enhanced", action="store_true", default=True, help="启用增强版功能：自适应突变、Pareto优化、收敛检测等。")
    enhanced_group.add_argument("--disable-enhanced", action="store_true", help="禁用增强版功能，使用传统算法。")
    enhanced_group.add_argument("--convergence-window", type=int, default=5, help="收敛检测的滑动窗口大小。")
    enhanced_group.add_argument("--convergence-threshold", type=float, default=0.001, help="收敛检测的分数方差阈值。")
    enhanced_group.add_argument("--max-stagnation", type=int, default=3, help="触发早停的最大停滞周期数。")
    enhanced_group.add_argument("--initial-temperature", type=float, default=1.0, help="自适应突变的初始温度。")
    enhanced_group.add_argument("--min-temperature", type=float, default=0.1, help="自适应突变的最小温度。")

    # --- 糖肽设计 (可选) ---
    glyco_group = parser.add_argument_group('糖肽设计')
    glyco_group.add_argument("--glycan_modification", type=str, default=None, help="通过提供糖肽修饰的CCD代码 (例如 'MANS') 来激活糖肽设计模式。仅在 --design_type=glycopeptide 时使用。")
    glyco_group.add_argument("--glycan_chain", type=str, default='C', help="在生成的YAML文件中分配给糖基配体的链ID。")
    glyco_group.add_argument("--modification_site", type=int, default=None, help="肽链序列上用于应用糖肽修饰的位置 (1-based索引)。")

    # --- 双环肽设计 (可选) ---
    bicyclic_group = parser.add_argument_group('双环肽设计')
    bicyclic_group.add_argument("--linker_ccd", type=str, default="SEZ", choices=["SEZ", "29N", "BS3"], help="用于形成双环的连接体配体的CCD代码。仅在 --design_type=bicyclic 时使用。")
    bicyclic_group.add_argument("--cys_positions", type=int, nargs=2, default=None, help="除末端外，另外两个半胱氨酸的初始位置(1-based索引)，例如 --cys_positions 4 10。如果未提供，将随机选择。仅在 --design_type=bicyclic 时使用。")

    # --- 氨基酸组成控制 ---
    amino_acid_group = parser.add_argument_group('氨基酸组成控制')
    amino_acid_group.add_argument("--no_cysteine", action="store_true", default=False, help="禁用半胱氨酸(Cys)，避免不必要的二硫键形成。")
    amino_acid_group.add_argument("--cyclic_binder", action="store_true", default=False, help="设计环状结合肽，通过N端和C端形成闭合环状结构。")

    # --- 输出与日志 ---
    output_group = parser.add_argument_group('输出与日志')
    output_group.add_argument("--output_csv", default=f"design_summary_{int(time.time())}.csv", help="输出所有评估设计结果的CSV汇总文件路径。")
    output_group.add_argument("--keep_temp_files", action="store_true", help="如果设置此项，则在运行结束后不删除临时工作目录。")
    
    # --- API 连接 ---
    api_group = parser.add_argument_group('API 连接')
    api_group.add_argument("--server_url", default="http://127.0.0.1:5000", help="Boltz-WebUI 预测 API 服务器的URL。")
    api_group.add_argument("--api_token", help="您的API密钥。也可以通过 'BOLTZ_API_TOKEN' 环境变量设置。")
    api_group.add_argument("--no_msa_server", action="store_true", default=False, help="禁用MSA服务器。默认情况下，当序列找不到MSA缓存时，会使用MSA服务器自动生成MSA以提高预测精度。")

    args = parser.parse_args()

    # 处理MSA服务器设置（默认启用，除非明确禁用）
    use_msa_server = not args.no_msa_server

    # --- 验证参数 ---
    logger.info("Validating command-line arguments...")
    # 糖肽设计验证
    if args.design_type == "glycopeptide":
        if not args.glycan_modification or not args.modification_site:
            parser.error("对于糖肽设计，--glycan_modification 和 --modification_site 都是必需的。")
        if not (1 <= args.modification_site <= args.binder_length):
            parser.error(f"--modification_site 必须是介于 1 和肽链长度 {args.binder_length} 之间的有效位置。")
    
    # 双环肽设计验证
    elif args.design_type == "bicyclic":
        if not args.linker_ccd:
            parser.error("对于双环肽设计，--linker_ccd 是必需的。")
        if args.cys_positions:
            if len(set(args.cys_positions)) != 2:
                parser.error("--cys_positions 必须提供两个不同的位置。")
            if any(p < 1 or p >= args.binder_length for p in args.cys_positions):
                parser.error(f"--cys_positions 的位置必须在 1 和 {args.binder_length - 1} 之间。")
            if args.binder_length in args.cys_positions:
                parser.error(f"末端位置 ({args.binder_length}) 由系统自动设为半胱氨酸，请不要在 --cys_positions 中指定。")
    
    if args.initial_binder_sequence and len(args.initial_binder_sequence) != args.binder_length:
        parser.error(f"--initial_binder_sequence 的长度 ({len(args.initial_binder_sequence)}) 必须与 --binder_length ({args.binder_length}) 匹配。")

    # sequence_mask验证
    if args.sequence_mask:
        # 移除可能的分隔符并验证长度
        mask_positions = args.sequence_mask.replace('-', '').replace('_', '').replace(' ', '')
        if len(mask_positions) != args.binder_length:
            parser.error(f"--sequence_mask 的长度 ({len(mask_positions)}) 必须与 --binder_length ({args.binder_length}) 匹配。")
        
        # 验证字符是否有效
        valid_chars = set('ACDEFGHIKLMNPQRSTVWYX')
        invalid_chars = set(mask_positions.upper()) - valid_chars
        if invalid_chars:
            parser.error(f"--sequence_mask 包含无效字符: {invalid_chars}。只允许标准氨基酸字符和X（表示可变位置）。")
        
        logger.info(f"Sequence mask validated: {args.sequence_mask}")

    # 半胱氨酸参数处理
    include_cysteine = not args.no_cysteine  # 简化为单一逻辑
    logger.info(f"Cysteine setting: {'enabled' if include_cysteine else 'disabled'}")

    if not np.isclose(args.weight_iptm + args.weight_plddt, 1.0):
        logger.warning(f"Weights for ipTM ({args.weight_iptm}) and pLDDT ({args.weight_plddt}) do not sum to 1.0. This is recommended but not strictly required.")

    api_token = args.api_token or os.environ.get('BOLTZ_API_TOKEN')
    if not api_token:
        logger.critical("API token is missing. Provide it via --api_token or the 'BOLTZ_API_TOKEN' environment variable.")
        raise ValueError("必须通过 --api_token 或 'BOLTZ_API_TOKEN' 环境变量提供API密钥。")
    
    logger.info("Arguments validated successfully.")

    # 加载用户约束（如果提供）
    user_constraints = []
    if args.user_constraints and os.path.exists(args.user_constraints):
        try:
            with open(args.user_constraints, 'r') as f:
                user_constraints = json.load(f)
            logger.info(f"Loaded {len(user_constraints)} user-defined constraints from {args.user_constraints}")
        except Exception as e:
            logger.warning(f"Failed to load user constraints from {args.user_constraints}: {e}")

    try:
        # 1. 初始化 API 客户端
        logger.info(f"Initializing API client for server: {args.server_url}")
        client = BoltzApiClient(server_url=args.server_url, api_token=api_token)

        # 2. 初始化 Designer
        logger.info(f"Initializing Designer with YAML template: {args.yaml_template}")
        if use_msa_server:
            logger.info("MSA server enabled: will use MSA server for sequences without cache")
        else:
            logger.info("MSA server disabled: will use empty MSA for sequences without cache")
        
        # 根据是否有糖肽或双环肽修饰选择合适的模型
        model_name = "boltz1" if args.design_type in ["glycopeptide"] else None
        if model_name:
            logger.info(f"{args.design_type.capitalize()} design detected - using model: {model_name}")
        
        designer = Designer(base_yaml_path=args.yaml_template, client=client, use_msa_server=use_msa_server, model_name=model_name)
        
        # 配置增强功能
        if hasattr(args, 'disable_enhanced') and args.disable_enhanced:
            designer.enable_enhanced_features = False
            logger.info("Enhanced features disabled - using traditional algorithms")
        else:
            designer.enable_enhanced_features = getattr(args, 'enable_enhanced', True)
            if designer.enable_enhanced_features:
                # 配置增强功能参数
                designer.convergence_window = getattr(args, 'convergence_window', 5)
                designer.convergence_threshold = getattr(args, 'convergence_threshold', 0.001)
                designer.max_stagnation = getattr(args, 'max_stagnation', 3)
                designer.temperature = getattr(args, 'initial_temperature', 1.0)
                designer.min_temperature = getattr(args, 'min_temperature', 0.1)
                logger.info("Enhanced features enabled with custom parameters")
            else:
                logger.info("Enhanced features disabled - using traditional algorithms")

        # 3. 开始设计任务
        design_kwargs = {
            'iterations': args.iterations,
            'population_size': args.population_size,
            'num_elites': args.num_elites,
            'binder_chain_id': args.binder_chain,
            'binder_length': args.binder_length,
            'initial_binder_sequence': args.initial_binder_sequence,
            'sequence_mask': args.sequence_mask,
            'mutation_rate': args.mutation_rate,
            'output_csv_path': args.output_csv,
            'keep_temp_files': args.keep_temp_files,
            'weight_iptm': args.weight_iptm,
            'weight_plddt': args.weight_plddt,
            'design_type': args.design_type,
            'include_cysteine': include_cysteine,  # 使用计算后的值
            'user_constraints': user_constraints,  # 新增：用户约束
            'cyclic_binder': args.cyclic_binder  # 新增：环状设计参数
        }

        if args.design_type == "glycopeptide":
            design_kwargs.update({
                'glycan_modification': args.glycan_modification,
                'glycan_chain_id': args.glycan_chain,
                'modification_site': args.modification_site - 1 if args.modification_site is not None else None,
            })
        elif args.design_type == "bicyclic":
            design_kwargs.update({
                'linker_ccd': args.linker_ccd,
                'cys_positions': [p - 1 for p in args.cys_positions] if args.cys_positions else None,
            })

        designer.run(**design_kwargs)

    except (ValueError, KeyError) as e:
        logger.critical(f"A critical configuration or value error occurred: {e}", exc_info=True)
    except Exception as e:
        logger.critical(f"An unexpected fatal error occurred during the design run: {e}", exc_info=True)


if __name__ == "__main__":
    main()