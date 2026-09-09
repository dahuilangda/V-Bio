import type { ProjectInputConfig } from '../../types/models';

export type WorkspaceTab = 'results' | 'basics' | 'components' | 'constraints';

export interface ProjectWorkspaceDraft {
  taskName: string;
  taskSummary: string;
  backend: string;
  use_msa: boolean;
  msa_mode?: 'none' | 'uniref' | 'env';
  color_mode: string;
  inputConfig: ProjectInputConfig;
}
