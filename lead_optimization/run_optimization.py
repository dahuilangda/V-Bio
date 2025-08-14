#!/usr/bin/env python3

"""
Lead optimization script - optimized version
Uses MMPDB + Boltz-WebUI for drug lead optimization
"""

import argparse
import os
import sys
import logging
import time
import json
from pathlib import Path
from typing import Optional, List, Dict, Any

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lead_optimization.config import OptimizationConfig, load_config
from lead_optimization.optimization_engine import OptimizationEngine, OptimizationResult
from lead_optimization.result_analyzer import OptimizationAnalyzer
from lead_optimization.exceptions import OptimizationError

def setup_logging(verbosity: int = 1):
    """Setup logging configuration"""
    log_levels = {
        0: logging.WARNING,
        1: logging.INFO,
        2: logging.DEBUG
    }
    
    level = log_levels.get(verbosity, logging.INFO)
    
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler('optimization.log')
        ]
    )

def validate_inputs(args) -> bool:
    """Validate input arguments"""
    # Check mutually exclusive inputs
    if args.input_compound and args.input_file:
        print("错误: --input_compound 和 --input_file 不能同时指定")
        return False
    
    if not args.input_compound and not args.input_file:
        print("错误: 必须指定 --input_compound 或 --input_file")
        return False
    
    # Check target protein file
    if not os.path.isfile(args.target_config):
        print(f"错误: 目标配置文件未找到: {args.target_config}")
        return False
    
    # Check input file if specified
    if args.input_file and not os.path.isfile(args.input_file):
        print(f"错误: 输入文件未找到: {args.input_file}")
        return False
    
    # Validate strategy
    valid_strategies = ['scaffold_hopping', 'fragment_replacement', 'multi_objective']
    if args.optimization_strategy not in valid_strategies:
        print(f"错误: 无效的优化策略. 可用策略: {', '.join(valid_strategies)}")
        return False
    
    return True

def create_output_directory(base_dir: str, prefix: str = "optimization") -> str:
    """Create unique output directory"""
    timestamp = int(time.time())
    output_dir = os.path.join(base_dir, f"{prefix}_{timestamp}")
    os.makedirs(output_dir, exist_ok=True)
    return output_dir

def run_single_optimization(engine: OptimizationEngine,
                          compound_smiles: str,
                          target_config: str,
                          strategy: str,
                          max_candidates: int,
                          output_dir: str,
                          iterations: int = 1,
                          batch_size: int = 4,
                          top_k_per_iteration: int = 5) -> OptimizationResult:
    """Run optimization for a single compound with iterative evolution"""
    logger = logging.getLogger(__name__)
    
    logger.info("=== 开始迭代化合物优化 ===")
    logger.info(f"输入化合物: {compound_smiles}")
    logger.info(f"优化策略: {strategy}")
    logger.info(f"迭代次数: {iterations}")
    logger.info(f"每轮最大候选数: {max_candidates}")
    logger.info(f"批次大小: {batch_size}")
    logger.info(f"每轮保留前K: {top_k_per_iteration}")
    
    try:
        result = engine.optimize_compound(
            compound_smiles=compound_smiles,
            target_protein_yaml=target_config,
            strategy=strategy,
            max_candidates=max_candidates,
            output_dir=output_dir,
            iterations=iterations,
            batch_size=batch_size,
            top_k_per_iteration=top_k_per_iteration
        )
        
        logger.info("=== 优化完成 ===")
        logger.info(f"生成候选化合物: {len(result.candidates)}")
        logger.info(f"成功评估: {len(result.scores)}")
        logger.info(f"执行时间: {result.execution_time:.2f}秒")
        
        if result.top_candidates:
            logger.info(f"最佳候选评分: {result.top_candidates[0]['combined_score']:.4f}")
            logger.info(f"最佳候选SMILES: {result.top_candidates[0]['smiles']}")
        
        return result
        
    except Exception as e:
        logger.error(f"优化失败: {e}")
        raise

def run_batch_optimization(engine: OptimizationEngine,
                         compounds_file: str,
                         target_config: str,
                         strategy: str,
                         max_candidates: int,
                         output_dir: str) -> Dict[str, OptimizationResult]:
    """Run optimization for multiple compounds"""
    logger = logging.getLogger(__name__)
    
    logger.info("=== 开始批量优化 ===")
    logger.info(f"输入文件: {compounds_file}")
    logger.info(f"优化策略: {strategy}")
    logger.info(f"每化合物最大候选数: {max_candidates}")
    
    try:
        results = engine.batch_optimize(
            compounds_file=compounds_file,
            target_protein_yaml=target_config,
            strategy=strategy,
            max_candidates=max_candidates,
            output_dir=output_dir
        )
        
        logger.info("=== 批量优化完成 ===")
        logger.info(f"处理化合物数: {len(results)}")
        
        # Statistics
        successful = len([r for r in results.values() if r.top_candidates])
        logger.info(f"成功优化: {successful}/{len(results)}")
        
        return results
        
    except Exception as e:
        logger.error(f"批量优化失败: {e}")
        raise

def save_results(results: Dict[str, Any], output_dir: str):
    """Save results to files (JSON summary only - real-time CSV already exists)"""
    logger = logging.getLogger(__name__)
    
    # Save summary JSON only (no final CSV needed)
    summary_file = os.path.join(output_dir, "optimization_summary.json")
    with open(summary_file, 'w') as f:
        json.dump(results, f, indent=2, default=str)
    
    logger.info(f"Optimization summary saved to: {summary_file}")
    logger.info(f"Real-time results are available in: {output_dir}/optimization_results_live.csv")

def main():
    parser = argparse.ArgumentParser(description="Lead Optimization using MMPDB + Boltz-WebUI")
    
    # Input options
    parser.add_argument("--input_compound", type=str, help="Input compound SMILES")
    parser.add_argument("--input_file", type=str, help="Input file (CSV or text) with compounds")
    parser.add_argument("--target_config", type=str, required=True, help="Target protein YAML config")
    
    # Optimization parameters
    parser.add_argument("--optimization_strategy", type=str, default="scaffold_hopping",
                       choices=["scaffold_hopping", "fragment_replacement", "multi_objective"],
                       help="Optimization strategy")
    parser.add_argument("--max_candidates", type=int, default=50,
                       help="Maximum candidates per iteration")
    parser.add_argument("--iterations", type=int, default=1,
                       help="Number of optimization iterations (genetic evolution)")
    parser.add_argument("--batch_size", type=int, default=4,
                       help="Number of compounds to submit to Boltz simultaneously")
    parser.add_argument("--top_k_per_iteration", type=int, default=5,
                       help="Top compounds to use as seeds for next iteration")
    
    # Output options
    parser.add_argument("--output_dir", type=str, help="Output directory")
    parser.add_argument("--generate_report", action="store_true", help="Generate HTML report")
    
    # System options
    parser.add_argument("--parallel_workers", type=int, default=1, 
                       help="Number of parallel workers for batch processing")
    parser.add_argument("--verbosity", type=int, default=1, choices=[0, 1, 2],
                       help="Logging verbosity (0=WARNING, 1=INFO, 2=DEBUG)")
    
    args = parser.parse_args()
    
    # Setup logging
    setup_logging(args.verbosity)
    logger = logging.getLogger(__name__)
    
    # Validate inputs
    if not validate_inputs(args):
        sys.exit(1)
    
    try:
        # Load configuration
        config = load_config()
        
        # Create output directory
        if not args.output_dir:
            args.output_dir = create_output_directory(".", "lead_optimization")
        else:
            os.makedirs(args.output_dir, exist_ok=True)
        
        logger.info(f"输出目录: {args.output_dir}")
        
        # Initialize engine
        engine = OptimizationEngine(config)
        
        # Run optimization
        results = {}
        
        if args.input_compound:
            # Single compound optimization
            result = run_single_optimization(
                engine=engine,
                compound_smiles=args.input_compound,
                target_config=args.target_config,
                strategy=args.optimization_strategy,
                max_candidates=args.max_candidates,
                output_dir=args.output_dir,
                iterations=args.iterations,
                batch_size=args.batch_size,
                top_k_per_iteration=args.top_k_per_iteration
            )
            results["single_compound"] = result
            
        else:
            # Batch optimization
            results = run_batch_optimization(
                engine=engine,
                compounds_file=args.input_file,
                target_config=args.target_config,
                strategy=args.optimization_strategy,
                max_candidates=args.max_candidates,
                output_dir=args.output_dir
            )
        
        # Save results
        save_results(results, args.output_dir)
        
        # Generate HTML report if requested
        if args.generate_report:
            try:
                # 准备优化数据用于分析器
                optimization_data = {
                    'original_compound': args.input_compound or 'batch_input',
                    'strategy': args.optimization_strategy,
                    'execution_time': 0,  # 这个值会从结果中获取
                    'statistics': {},
                    'top_candidates': []
                }
                
                # 从results中提取数据
                if args.input_compound and 'single_compound' in results:
                    result = results['single_compound']
                    optimization_data['execution_time'] = result.execution_time
                    optimization_data['top_candidates'] = result.top_candidates or []
                    optimization_data['statistics'] = {
                        'total_candidates': len(result.candidates),
                        'successful_evaluations': len(result.scores),
                        'success_rate': len(result.scores) / len(result.candidates) if result.candidates else 0
                    }
                else:
                    # 批处理结果
                    all_candidates = []
                    total_time = 0
                    total_candidates = 0
                    successful_evaluations = 0
                    
                    for compound_id, result in results.items():
                        if hasattr(result, 'top_candidates') and result.top_candidates:
                            all_candidates.extend(result.top_candidates)
                        if hasattr(result, 'execution_time'):
                            total_time += result.execution_time
                        if hasattr(result, 'candidates'):
                            total_candidates += len(result.candidates)
                        if hasattr(result, 'scores'):
                            successful_evaluations += len(result.scores)
                    
                    # 按评分排序并取前10个
                    all_candidates.sort(key=lambda x: x.get('combined_score', 0), reverse=True)
                    optimization_data['top_candidates'] = all_candidates[:10]
                    optimization_data['execution_time'] = total_time
                    optimization_data['statistics'] = {
                        'total_compounds': len(results),
                        'total_candidates': total_candidates,
                        'successful_evaluations': successful_evaluations,
                        'success_rate': successful_evaluations / total_candidates if total_candidates else 0
                    }
                
                # 初始化分析器
                analyzer = OptimizationAnalyzer(optimization_data, args.output_dir)
                
                # 生成图表
                plots = analyzer.generate_optimization_plots()
                logger.info(f"成功生成 {len(plots)} 个分析图表")
                
                # 保存CSV结果
                analyzer.save_results_to_csv()
                logger.info("分析结果已保存为CSV格式")
                
                # 生成HTML报告
                html_report_path = analyzer.generate_html_report(results)
                logger.info(f"HTML报告已生成: {html_report_path}")
                
                logger.info(f"优化分析完成，结果保存在: {args.output_dir}")
                
            except Exception as e:
                logger.warning(f"生成分析报告失败: {e}")
                import traceback
                logger.debug(f"详细错误信息: {traceback.format_exc()}")
        
        logger.info("=== 优化任务完成 ===")
        print(f"结果保存在: {args.output_dir}")
        
    except Exception as e:
        logger.error(f"优化任务失败: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
