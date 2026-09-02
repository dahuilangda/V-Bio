export { submitPrediction } from './backendPredictionApi';

export {
  fetchLeadOptimizationHaloBackends,
  fetchLeadOptimizationHaloStatus,
  previewLeadOptimizationFragments,
  previewLeadOptimizationReference,
  submitLeadOptimizationHaloOptimize,
} from './backendLeadOptimizationApi';
export type {
  LeadOptFragmentPreviewResponse,
  LeadOptHaloBackend,
  LeadOptHaloBackendsResponse,
  LeadOptHaloMode,
  LeadOptHaloOptimizeInput,
  LeadOptHaloOptimizeResponse,
  LeadOptHaloRoundEvent,
  LeadOptHaloStatusResponse,
  LeadOptReferencePreviewResponse
} from './backendLeadOptimizationApi';

export { previewAffinityComplex, submitAffinityScoring } from './backendAffinityApi';

export { downloadResultBlob, getTaskRuntimeIndex, getTaskStatus, getTaskStatuses, terminateTask } from './backendTaskApi';
export type { DownloadResultMode, TaskRuntimeIndexResponse } from './backendTaskApi';

export {
  compactResultConfidenceForStorage,
  downloadResultFile,
  ensureStructureConfidenceColoringData,
  stripStructureConfidenceColoringData,
  parseResultBundle
} from './backendResultParserApi';
