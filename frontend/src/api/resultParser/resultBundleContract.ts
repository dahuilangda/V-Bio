import type { ParsedResultBundle } from '../../types/models';

export interface ParseResultBundleOptions {
  preservePeptideCandidateStructureText?: boolean;
  preferredStructureName?: string;
}

export interface ParseResultBundlePayload {
  blob: Blob;
  options?: ParseResultBundleOptions;
}

export type ParseResultBundleResult = ParsedResultBundle | null;
