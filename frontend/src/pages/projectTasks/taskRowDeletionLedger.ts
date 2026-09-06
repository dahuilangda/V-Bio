import type { ProjectTask } from '../../types/models';

/**
 * Deletion ledger for project task rows.
 *
 * The task list is filled by several concurrent async writers (initial load,
 * background pagination chunks, runtime-status polls, result hydration). Each
 * of them can hold a snapshot that was captured before a user deletion
 * committed; without coordination those snapshots would merge an already
 * deleted row back into the list. The ledger records server-confirmed
 * deletions, and every writer filters its rows through `apply` before writing
 * list state or the runtime cache — the same discipline TanStack Query applies
 * to optimistic mutations (a query response in flight when the mutation
 * committed must not overwrite the mutation's result).
 *
 * Membership is unconditional: task row ids are uuid primary keys, so a
 * deleted row can never legitimately reappear in a later response and no
 * expiry heuristic is needed. `reset` runs when the loader switches projects.
 */
export class TaskRowDeletionLedger {
  private readonly deletedRowIds = new Set<string>();

  /** Record a deletion confirmed by the server. Empty ids are ignored. */
  markDeleted(taskRowId: string): void {
    const rowId = String(taskRowId || '').trim();
    if (rowId) this.deletedRowIds.add(rowId);
  }

  isDeleted(taskRowId: string): boolean {
    return this.deletedRowIds.has(String(taskRowId || '').trim());
  }

  /**
   * Drop rows whose deletion was confirmed after the caller captured its
   * snapshot. Returns the input array reference untouched while the ledger is
   * empty so hot paths (poll ticks, chunk merges) pay nothing before the
   * first deletion.
   */
  apply(rows: ProjectTask[]): ProjectTask[] {
    if (this.deletedRowIds.size === 0) return rows;
    return rows.filter((row) => !this.deletedRowIds.has(String(row?.id || '').trim()));
  }

  /** Forget all deletions — used when the loader switches to another project. */
  reset(): void {
    this.deletedRowIds.clear();
  }
}
