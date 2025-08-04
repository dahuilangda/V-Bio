# /Boltz-WebUI/virtual_screening/run_screening.py

"""
run_screening.py

该脚本是虚拟筛选工具的命令行界面（CLI）入口点。

功能：
- 解析命令行参数以配置筛选任务
- 设置全局日志记录系统
- 验证用户输入
- 初始化API客户端和筛选引擎
- 启动并管理整个筛选流程
"""

import argparse
import os
import time
import logging
import sys
import shutil
import json
from pathlib import Path

# 本地模块导入
from api_client import BoltzApiClient
from screening_engine import SimpleScreeningEngine, ScreeningConfig
from molecule_library import LibraryProcessor

def setup_logging(log_level: str = "INFO"):
    """配置全局日志记录"""
    log_levels = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR
    }
    
    logging.basicConfig(
        level=log_levels.get(log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        stream=sys.stdout,
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 为第三方库设置较高的日志级别，减少噪音
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)

def validate_inputs(args) -> bool:
    """验证输入参数"""
    logger = logging.getLogger(__name__)
    
    # 检查目标文件
    if not os.path.exists(args.target):
        logger.error(f"目标配置文件不存在: {args.target}")
        return False
    
    # 检查分子库文件
    if not os.path.exists(args.library):
        logger.error(f"分子库文件不存在: {args.library}")
        return False
    
    # 检查分子库类型
    valid_types = ["peptide", "small_molecule", "auto"]
    if args.library_type not in valid_types:
        logger.error(f"不支持的分子库类型: {args.library_type}，支持的类型: {valid_types}")
        return False
    
    # 检查权重值范围
    weights = [args.binding_affinity_weight, args.structural_stability_weight, args.confidence_weight]
    if any(w < 0 for w in weights):
        logger.error("权重值不能为负数")
        return False
    
    if sum(weights) == 0:
        logger.error("所有权重不能都为0")
        return False
    
    # 改进的输出目录检查和续算逻辑
    if os.path.exists(args.output_dir) and os.listdir(args.output_dir):
        # 检查是否有筛选相关的文件
        results_file = os.path.join(args.output_dir, "screening_results_complete.csv")
        tasks_dir = os.path.join(args.output_dir, "tasks")
        config_file = os.path.join(args.output_dir, "screening_config.json")
        temp_configs_dir = os.path.join(args.output_dir, "temp_configs")
        
        # 检查是否有任何筛选相关的文件或目录
        has_screening_files = (
            os.path.exists(results_file) or
            os.path.exists(tasks_dir) or
            os.path.exists(config_file) or
            os.path.exists(temp_configs_dir)
        )
        
        if has_screening_files:
            # 存在筛选相关文件，支持续算
            logger.info(f"检测到现有筛选记录: {args.output_dir}")
            
            if os.path.exists(results_file):
                logger.info("发现完整的筛选结果文件，将启用续算模式（自动跳过已完成分子）")
            elif os.path.exists(tasks_dir) and os.listdir(tasks_dir):
                logger.info("发现任务文件，将启用续算模式（自动跳过已完成分子）")
            else:
                logger.info("发现筛选配置文件，将启用续算模式")
            
            if args.force:
                # 如果使用 --force，清除现有目录内容
                logger.info(f"使用 --force 选项，清除现有输出目录内容: {args.output_dir}")
                try:
                    shutil.rmtree(args.output_dir)
                    os.makedirs(args.output_dir, exist_ok=True)
                    logger.info(f"输出目录已清除并重新创建: {args.output_dir}")
                except Exception as e:
                    logger.error(f"清除输出目录失败: {e}")
                    return False
        else:
            # 目录非空但没有筛选记录（可能是其他不相关的文件）
            if not args.force:
                logger.error(f"输出目录非空且包含非筛选相关文件: {args.output_dir}，使用 --force 覆盖现有结果")
                return False
            else:
                # 如果使用 --force，清除现有目录内容
                logger.info(f"使用 --force 选项，清除现有输出目录内容: {args.output_dir}")
                try:
                    shutil.rmtree(args.output_dir)
                    os.makedirs(args.output_dir, exist_ok=True)
                    logger.info(f"输出目录已清除并重新创建: {args.output_dir}")
                except Exception as e:
                    logger.error(f"清除输出目录失败: {e}")
                    return False
    
    return True

def create_config_from_args(args) -> ScreeningConfig:
    """从命令行参数创建配置对象"""
    # 构建评分权重字典
    scoring_weights = {
        "binding_affinity": args.binding_affinity_weight,
        "structural_stability": args.structural_stability_weight,
        "confidence": args.confidence_weight
    }
    
    # 归一化权重
    total_weight = sum(scoring_weights.values())
    if total_weight > 0:
        scoring_weights = {k: v/total_weight for k, v in scoring_weights.items()}
    
    # 自动检测分子库类型
    library_type = args.library_type
    if library_type == "auto":
        ext = Path(args.library).suffix.lower()
        if ext in ['.fasta', '.fa', '.fas']:
            library_type = "peptide"
        elif ext in ['.sdf', '.mol', '.csv', '.smi', '.smiles']:
            library_type = "small_molecule"
        else:
            raise ValueError(f"无法从文件扩展名 {ext} 自动推断分子库类型")
    
    config = ScreeningConfig(
        target_yaml=args.target,
        library_path=args.library,
        library_type=library_type,
        output_dir=args.output_dir,
        max_molecules=args.max_molecules,
        batch_size=args.batch_size,
        max_workers=args.max_workers,
        timeout=args.timeout,
        retry_attempts=args.retry_attempts,
        scoring_weights=scoring_weights,
        min_binding_score=args.min_binding_score,
        top_n=args.top_n,
        use_msa_server=args.use_msa_server,
        save_structures=args.save_structures,
        generate_plots=args.generate_plots,
        auto_enable_affinity=args.auto_enable_affinity,
        enable_affinity=args.enable_affinity
    )
    
    return config

def test_server_connection(client: BoltzApiClient) -> bool:
    """测试服务器连接"""
    logger = logging.getLogger(__name__)
    
    try:
        logger.info("测试服务器连接...")
        status = client.get_server_status()
        
        if status.get("status") == "error":
            logger.error(f"服务器连接失败: {status.get('message')}")
            return False
        
        logger.info("服务器连接正常")
        return True
        
    except Exception as e:
        logger.error(f"服务器连接测试失败: {e}")
        return False

def print_config_summary(config: ScreeningConfig):
    """打印配置摘要"""
    logger = logging.getLogger(__name__)
    
    logger.info("=" * 60)
    logger.info("虚拟筛选配置摘要")
    logger.info("=" * 60)
    logger.info(f"目标配置文件: {config.target_yaml}")
    logger.info(f"分子库文件: {config.library_path}")
    logger.info(f"分子库类型: {config.library_type}")
    logger.info(f"输出目录: {config.output_dir}")
    logger.info(f"最大分子数: {config.max_molecules if config.max_molecules > 0 else '全部'}")
    logger.info(f"批次大小: {config.batch_size}")
    logger.info(f"并行工作线程: {config.max_workers}")
    logger.info(f"保留顶部结果: {config.top_n}")
    logger.info(f"使用MSA服务器: {config.use_msa_server}")
    logger.info("评分权重:")
    for name, weight in config.scoring_weights.items():
        logger.info(f"  {name}: {weight:.2f}")
    logger.info("=" * 60)

def main():
    """主执行函数：解析参数并启动筛选任务"""
    parser = argparse.ArgumentParser(
        description="使用 Boltz-WebUI API 运行虚拟筛选任务",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="""
示例用法:
  # 多肽筛选
  python run_screening.py --target target.yaml --library peptides.fasta --library_type peptide --output_dir results
  
  # 小分子筛选
  python run_screening.py --target target.yaml --library compounds.sdf --library_type small_molecule --output_dir results
  
  # 自定义评分权重
  python run_screening.py --target target.yaml --library library.csv --scoring_weights '{"binding_affinity":0.7,"structural_stability":0.2,"confidence":0.1}'
  
  # 智能续算（自动检测未完成任务并继续）
  python run_screening.py --target target.yaml --library library.csv --output_dir results
        """
    )
    
    # --- 必需参数 ---
    required = parser.add_argument_group('必需参数')
    required.add_argument(
        "--target", "-t",
        required=True,
        help="目标蛋白的YAML配置文件路径"
    )
    required.add_argument(
        "--library", "-l",
        required=True,
        help="分子库文件路径 (支持FASTA、SDF、CSV、SMILES等格式)"
    )
    required.add_argument(
        "--output_dir", "-o",
        required=True,
        help="结果输出目录"
    )
    
    # --- 基本配置 ---
    basic = parser.add_argument_group('基本配置')
    basic.add_argument(
        "--library_type",
        choices=["peptide", "small_molecule", "auto"],
        default="auto",
        help="分子库类型 (auto=自动检测)"
    )
    basic.add_argument(
        "--server_url",
        default="http://localhost:5000",
        help="Boltz-WebUI服务器地址"
    )
    basic.add_argument(
        "--api_token",
        default="",
        help="API访问令牌"
    )
    
    # --- 筛选参数 ---
    screening = parser.add_argument_group('筛选参数')
    screening.add_argument(
        "--max_molecules",
        type=int,
        default=-1,
        help="最大筛选分子数 (-1=全部)"
    )
    screening.add_argument(
        "--batch_size",
        type=int,
        default=50,
        help="批处理大小"
    )
    screening.add_argument(
        "--max_workers",
        type=int,
        default=4,
        help="并行工作线程数"
    )
    screening.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="单个任务超时时间（秒）"
    )
    screening.add_argument(
        "--retry_attempts",
        type=int,
        default=3,
        help="失败重试次数"
    )
    screening.add_argument(
        "--use_msa_server",
        action="store_true",
        help="当序列找不到MSA缓存时使用MSA服务器"
    )
    
    # --- 评分权重参数 ---
    scoring = parser.add_argument_group('评分权重参数')
    scoring.add_argument(
        "--binding_affinity_weight",
        type=float,
        default=0.6,
        help="结合亲和力权重"
    )
    scoring.add_argument(
        "--structural_stability_weight",
        type=float,
        default=0.2,
        help="结构稳定性权重"
    )
    scoring.add_argument(
        "--confidence_weight",
        type=float,
        default=0.2,
        help="预测置信度权重"
    )
    
    # --- 分子过滤参数 ---
    filters = parser.add_argument_group('分子过滤参数')
    filters.add_argument(
        "--min_binding_score",
        type=float,
        default=0.0,
        help="最小结合评分阈值"
    )
    
    # --- 输出设置 ---
    output = parser.add_argument_group('输出设置')
    output.add_argument(
        "--top_n",
        type=int,
        default=100,
        help="保留的顶部结果数量"
    )
    output.add_argument(
        "--save_structures",
        action="store_true",
        default=True,
        help="保存预测的复合物结构"
    )
    output.add_argument(
        "--generate_plots",
        action="store_true",
        default=True,
        help="生成结果分析图表"
    )
    output.add_argument(
        "--report_only",
        action="store_true",
        default=False,
        help="仅重新生成报告和CSV文件（基于现有结果）"
    )
    
    # --- 高级选项 ---
    advanced = parser.add_argument_group('高级选项')
    advanced.add_argument(
        "--auto_enable_affinity",
        action="store_true",
        default=True,
        help="自动启用亲和力计算（基于分子类型检测）"
    )
    advanced.add_argument(
        "--enable_affinity",
        action="store_true",
        default=False,
        help="强制启用亲和力计算"
    )
    
    # --- 其他选项 ---
    other = parser.add_argument_group('其他选项')
    other.add_argument(
        "--log_level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="日志级别"
    )
    other.add_argument(
        "--force",
        action="store_true",
        help="强制覆盖现有输出目录"
    )
    other.add_argument(
        "--config_file",
        help="从配置文件加载参数"
    )
    other.add_argument(
        "--dry_run",
        action="store_true",
        help="仅验证配置，不执行筛选"
    )
    
    args = parser.parse_args()
    
    # 设置日志
    setup_logging(args.log_level)
    logger = logging.getLogger(__name__)
    
    try:
        logger.info("虚拟筛选工具启动")
        start_time = time.time()
        
        # 验证输入
        if not validate_inputs(args):
            logger.error("输入验证失败")
            sys.exit(1)
        
        # 创建配置
        config = create_config_from_args(args)
        print_config_summary(config)
        
        # 干运行模式
        if args.dry_run:
            logger.info("干运行模式：配置验证通过，未执行实际筛选")
            sys.exit(0)
        
        # 初始化API客户端
        client = BoltzApiClient(args.server_url, args.api_token)
        
        # 测试服务器连接
        if not test_server_connection(client):
            logger.error("无法连接到Boltz-WebUI服务器")
            sys.exit(1)
        
        # 创建输出目录
        os.makedirs(config.output_dir, exist_ok=True)
        
        # 保存配置到输出目录
        config_path = os.path.join(config.output_dir, "screening_config.json")
        with open(config_path, 'w') as f:
            # 将配置对象转换为字典并保存
            config_dict = {
                "target_yaml": config.target_yaml,
                "library_path": config.library_path,
                "library_type": config.library_type,
                "output_dir": config.output_dir,
                "max_molecules": config.max_molecules,
                "batch_size": config.batch_size,
                "max_workers": config.max_workers,
                "timeout": config.timeout,
                "retry_attempts": config.retry_attempts,
                "scoring_weights": config.scoring_weights,
                "min_binding_score": config.min_binding_score,
                "top_n": config.top_n,
                "use_msa_server": config.use_msa_server,
                "save_structures": config.save_structures,
                "generate_plots": config.generate_plots
            }
            json.dump(config_dict, f, indent=2, ensure_ascii=False)
        
        logger.info(f"配置已保存到: {config_path}")
        
        # 初始化筛选引擎
        engine = SimpleScreeningEngine(client, config)
        
        # 检查是否只需要重新生成报告
        if args.report_only:
            logger.info("仅重新生成报告模式")
            
            # 检查是否有现有结果
            results_file = os.path.join(config.output_dir, "screening_results_complete.csv")
            tasks_dir = os.path.join(config.output_dir, "tasks")
            
            if not os.path.exists(results_file) and not (os.path.exists(tasks_dir) and os.listdir(tasks_dir)):
                logger.error("没有找到现有结果文件，无法重新生成报告")
                logger.error("请先运行完整的筛选或确保结果目录包含有效的筛选结果")
                sys.exit(1)
            
            # 加载现有结果
            logger.info("加载现有结果...")
            existing_results = engine._load_existing_results()
            if existing_results:
                engine.screening_results = existing_results
                logger.info(f"成功加载 {len(existing_results)} 个结果")
                
                # 重新生成报告和文件
                logger.info("重新生成报告...")
                engine._process_and_save_results()
                
                # 获取摘要
                summary = engine.get_screening_summary()
                
                logger.info("=" * 60)
                logger.info("报告重新生成完成！")
                logger.info("=" * 60)
                logger.info(f"筛选分子数: {summary['total_screened']}")
                logger.info(f"成功预测: {summary['successful_predictions']}")
                logger.info(f"失败预测: {summary['failed_predictions']}")
                logger.info(f"成功率: {summary['success_rate']:.2%}")
                logger.info(f"最高评分: {summary['top_score']:.4f}")
                logger.info(f"结果目录: {config.output_dir}")
                logger.info("=" * 60)
                sys.exit(0)
            else:
                logger.error("未能加载到有效的结果数据")
                sys.exit(1)
        
        # 运行筛选
        logger.info("开始执行虚拟筛选...")
        success = engine.run_screening()
        
        if success:
            # 获取筛选摘要
            summary = engine.get_screening_summary()
            
            elapsed_time = time.time() - start_time
            logger.info("=" * 60)
            logger.info("虚拟筛选完成！")
            logger.info("=" * 60)
            logger.info(f"总用时: {elapsed_time:.2f} 秒")
            logger.info(f"筛选分子数: {summary['total_screened']}")
            logger.info(f"成功预测: {summary['successful_predictions']}")
            logger.info(f"失败预测: {summary['failed_predictions']}")
            logger.info(f"成功率: {summary['success_rate']:.2%}")
            logger.info(f"最高评分: {summary['top_score']:.4f}")
            logger.info(f"结果目录: {config.output_dir}")
            logger.info("=" * 60)
            
            # 打印主要输出文件
            logger.info("主要输出文件:")
            output_files = [
                "screening_summary.json",
                "screening_results_complete.csv",
                "top_hits.csv",
                "analysis_report.html"
            ]
            
            for filename in output_files:
                filepath = os.path.join(config.output_dir, filename)
                if os.path.exists(filepath):
                    logger.info(f"  - {filename}")
            
            if os.path.exists(os.path.join(config.output_dir, "plots")):
                logger.info(f"  - plots/ (分析图表)")
            
            logger.info("虚拟筛选成功完成！")
            sys.exit(0)
        else:
            logger.error("虚拟筛选失败")
            sys.exit(1)
            
    except KeyboardInterrupt:
        logger.warning("用户中断了筛选过程")
        sys.exit(1)
    except Exception as e:
        logger.error(f"虚拟筛选过程中发生未预期的错误: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()