export { submitPrediction } from './backendPredictionApi';

export {
  previewLeadOptimizationFragments,
  previewLeadOptimizationReference,
} from './backendLeadOptimizationApi';

export { previewAffinityComplex, submitAffinityScoring } from './backendAffinityApi';

export { downloadResultBlob, getTaskRuntimeIndex, getTaskStatus, getTaskStatuses, terminateTask } from './backendTaskApi';
export type { TaskRuntimeIndexResponse } from './backendTaskApi';

export {
  downloadResultFile,
  ensureStructureConfidenceColoringData,
  stripStructureConfidenceColoringData,
  parseResultBundle
} from './backendResultParserApi';
