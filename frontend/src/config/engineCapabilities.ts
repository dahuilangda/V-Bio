/**
 * 引擎能力矩阵：单一事实源在仓库根 configs/engine_capabilities.json，
 * 这里直接导入该文件，前后端共用同一份数据（契约测试防漂移）。
 * 新增/调整引擎只改 configs JSON，选项列表自动跟随。
 */
import matrix from '../../../configs/engine_capabilities.json';

export type WorkflowKey =
  | 'prediction'
  | 'peptide_design'
  | 'virtual_screening'
  | 'lead_optimization';

export type Chirality = 'l' | 'd';
export type DesignMode = 'linear' | 'cyclic' | 'bicyclic';

/** 肽设计工作流内引擎承担的角色：design = 提案器（生成候选），score = 打分器（验证候选）。 */
export type PeptideRole = 'design' | 'score';

export interface EngineFeatures {
  pocket: boolean;
  custom_ccd: boolean;
  msa: boolean;
  ncaa: boolean;
}

export interface EngineCapability {
  label: string;
  status: 'active' | 'deprecated';
  workflows: WorkflowKey[];
  chirality: Chirality[];
  design_modes: DesignMode[];
  peptide_roles?: PeptideRole[];
  features: EngineFeatures;
}

export const ENGINES = matrix.engines as Record<string, EngineCapability>;

export function activeEngineIds(): string[] {
  return Object.entries(ENGINES)
    .filter(([, engine]) => engine.status === 'active')
    .map(([id]) => id);
}

export interface EngineOption {
  value: string;
  label: string;
}

/** 按 workflow（可选 chirality/designMode）过滤出可用的引擎下拉项。 */
export function engineOptionsFor(options: {
  workflow: WorkflowKey;
  chirality?: Chirality;
  designMode?: DesignMode;
}): EngineOption[] {
  return Object.entries(ENGINES)
    .filter(([, engine]) => {
      if (engine.status !== 'active') return false;
      if (!engine.workflows.includes(options.workflow)) return false;
      if (options.chirality && engine.chirality.length > 0
        && !engine.chirality.includes(options.chirality)) return false;
      if (options.designMode && engine.design_modes.length > 0
        && !engine.design_modes.includes(options.designMode)) return false;
      return true;
    })
    .map(([id, engine]) => ({ value: id, label: engine.label }));
}

/** 肽设计工作流按角色（design/score）过滤引擎；未声明角色的引擎不参与肽设计下拉。 */
export function peptideEngineOptionsFor(options: {
  role: PeptideRole;
  chirality: Chirality;
  designMode: DesignMode;
}): EngineOption[] {
  return Object.entries(ENGINES)
    .filter(([, engine]) => {
      if (engine.status !== 'active') return false;
      if (!engine.workflows.includes('peptide_design')) return false;
      if (!(engine.peptide_roles || []).includes(options.role)) return false;
      if (engine.chirality.length > 0 && !engine.chirality.includes(options.chirality)) return false;
      if (engine.design_modes.length > 0 && !engine.design_modes.includes(options.designMode)) return false;
      return true;
    })
    .map(([id, engine]) => ({ value: id, label: engine.label }));
}

export function engineSupportsPeptideRole(backend: string, role: PeptideRole): boolean {
  return (ENGINES[backend]?.peptide_roles || []).includes(role) === true;
}

export function engineSupportsWorkflow(backend: string, workflow: WorkflowKey): boolean {
  return ENGINES[backend]?.workflows.includes(workflow) === true;
}

export function engineSupportsDesignMode(backend: string, mode: DesignMode): boolean {
  return ENGINES[backend]?.design_modes.includes(mode) === true;
}

export function engineSupportsFeature(backend: string, feature: keyof EngineFeatures): boolean {
  return Boolean(ENGINES[backend]?.features?.[feature]);
}
