import { listProjectTasks } from '../../api/supabaseLite';
import type { Project } from '../../types/models';
import { formatDateTime, formatDuration } from '../../utils/date';
import { renderLigand2DSvg } from '../../utils/ligand2d';
import { loadRDKitModule } from '../../utils/rdkit';
import {
  readTaskLigandAtomPlddts,
  resolveTaskSelectionContext,
  sanitizeFileName,
  toBase64FromBytes
} from './taskDataUtils';
import type { TaskListRow, WorkspacePairPreference } from './taskListTypes';
import { backendLabel, formatMetric, taskStateLabel } from './taskPresentation';

interface ExportTaskRowsToExcelInput {
  project: Project;
  filteredRows: TaskListRow[];
  workspacePairPreference: WorkspacePairPreference;
}

export async function exportTaskRowsToExcel({
  project,
  filteredRows,
  workspacePairPreference
}: ExportTaskRowsToExcelInput): Promise<void> {
  if (filteredRows.length === 0) return;

  const [{ default: ExcelJS }, rdkit] = await Promise.all([import('exceljs'), loadRDKitModule()]);
  const authoritativeTasks = await listProjectTasks(project.id);
  const authoritativeTaskMap = new Map(authoritativeTasks.map((task) => [task.id, task] as const));
  const imageWidthPx = 220;
  const imageHeightPx = 132;
  const rowHeightPt = Math.round((imageHeightPx * 72) / 96);
  const sheetName = (() => {
    const normalized = String(project.name || 'Tasks')
      .replace(/[\\/*?:[\]]/g, ' ')
      .trim();
    const truncated = normalized.slice(0, 31);
    return truncated || 'Tasks';
  })();

  const toPngBytesFromSvg = async (svg: string): Promise<Uint8Array> => {
    const svgBlob = new Blob([svg], { type: 'image/svg+xml;charset=utf-8' });
    const imageUrl = URL.createObjectURL(svgBlob);
    const image = await new Promise<HTMLImageElement>((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error('Failed to decode SVG image.'));
      img.src = imageUrl;
    });
    try {
      const canvas = document.createElement('canvas');
      canvas.width = imageWidthPx;
      canvas.height = imageHeightPx;
      const ctx = canvas.getContext('2d');
      if (!ctx) {
        throw new Error('Canvas 2D context is unavailable.');
      }
      ctx.fillStyle = '#ffffff';
      ctx.fillRect(0, 0, imageWidthPx, imageHeightPx);
      ctx.drawImage(image, 0, 0, imageWidthPx, imageHeightPx);
      const pngBlob = await new Promise<Blob>((resolve, reject) => {
        canvas.toBlob((blob) => {
          if (!blob) {
            reject(new Error('Failed to encode PNG image.'));
            return;
          }
          resolve(blob);
        }, 'image/png');
      });
      return new Uint8Array(await pngBlob.arrayBuffer());
    } finally {
      URL.revokeObjectURL(imageUrl);
    }
  };

  const workbook = new ExcelJS.Workbook();
  const worksheet = workbook.addWorksheet(sheetName);
  worksheet.columns = [
    { header: '#', key: 'index', width: 6 },
    { header: 'Task Name', key: 'taskName', width: 24 },
    { header: 'Task Summary', key: 'taskSummary', width: 36 },
    { header: 'Task Row ID', key: 'taskRowId', width: 38 },
    { header: 'Runtime Task ID', key: 'runtimeTaskId', width: 24 },
    { header: 'State', key: 'state', width: 12 },
    { header: 'Backend', key: 'backend', width: 12 },
    { header: 'Submitted', key: 'submitted', width: 22 },
    { header: 'Duration', key: 'duration', width: 12 },
    { header: 'pLDDT', key: 'plddt', width: 10 },
    { header: 'iPTM', key: 'iptm', width: 10 },
    { header: 'PAE', key: 'pae', width: 10 },
    { header: 'SMILES', key: 'smiles', width: 42 },
    { header: 'Ligand 2D (Confidence Color)', key: 'ligand2d', width: 36 }
  ];
  worksheet.getRow(1).font = { bold: true };

  let ligandSmilesRowCount = 0;
  let renderedImageCount = 0;
  let firstRenderError: string | null = null;

  for (let i = 0; i < filteredRows.length; i += 1) {
    const row = filteredRows[i];
    const { metrics } = row;
    const task = authoritativeTaskMap.get(row.task.id) || row.task;
    const selection = resolveTaskSelectionContext(task, workspacePairPreference);
    const ligandSmiles = selection.ligandSmiles;
    const ligandIsSmiles = selection.ligandIsSmiles;
    const ligandAtomPlddts =
      readTaskLigandAtomPlddts(task, selection.ligandChainId, selection.ligandComponentCount <= 1) ??
      row.ligandAtomPlddts;
    const submittedText = formatDateTime(task.submitted_at || task.created_at);
    const runtimeTaskId = String(task.task_id || '').trim();
    let imageBytes: Uint8Array | null = null;
    if (ligandSmiles && ligandIsSmiles) {
      ligandSmilesRowCount += 1;
      try {
        const svg = renderLigand2DSvg(rdkit, {
          smiles: ligandSmiles,
          width: imageWidthPx,
          height: imageHeightPx,
          atomConfidences: ligandAtomPlddts,
          confidenceHint: metrics.plddt
        });
        imageBytes = await toPngBytesFromSvg(svg);
        renderedImageCount += 1;
      } catch (err) {
        imageBytes = null;
        if (!firstRenderError) {
          const reason = err instanceof Error && err.message ? err.message : 'unknown error';
          firstRenderError = `task ${task.id} (${reason})`;
        }
      }
    }
    const excelRow = worksheet.addRow({
      index: i + 1,
      taskName: String(task.name || '').trim(),
      taskSummary: String(task.summary || '').trim(),
      taskRowId: task.id,
      runtimeTaskId,
      state: taskStateLabel(task.task_state),
      backend: backendLabel(task.backend || ''),
      submitted: submittedText,
      duration: formatDuration(task.duration_seconds),
      plddt: formatMetric(metrics.plddt, 1),
      iptm: formatMetric(metrics.iptm, 3),
      pae: formatMetric(metrics.pae, 2),
      smiles: ligandSmiles || '',
      ligand2d: imageBytes ? '' : '-'
    });
    if (imageBytes) {
      excelRow.height = rowHeightPt;
      const imageId = workbook.addImage({
        base64: `data:image/png;base64,${toBase64FromBytes(imageBytes)}`,
        extension: 'png'
      });
      worksheet.addImage(imageId, {
        tl: { col: 13, row: excelRow.number - 1 },
        ext: { width: imageWidthPx, height: imageHeightPx },
        editAs: 'oneCell'
      });
    }
  }
  if (ligandSmilesRowCount > 0 && renderedImageCount === 0) {
    throw new Error(
      firstRenderError
        ? `Ligand 2D rendering failed for all SMILES rows (${ligandSmilesRowCount}). First failure: ${firstRenderError}.`
        : `Ligand 2D rendering failed for all SMILES rows (${ligandSmilesRowCount}).`
    );
  }

  const workbookBuffer = await workbook.xlsx.writeBuffer();
  const blob = new Blob([workbookBuffer], {
    type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
  });
  const now = new Date();
  const timestamp = [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, '0'),
    String(now.getDate()).padStart(2, '0'),
    '_',
    String(now.getHours()).padStart(2, '0'),
    String(now.getMinutes()).padStart(2, '0'),
    String(now.getSeconds()).padStart(2, '0')
  ].join('');
  const fileName = `${sanitizeFileName(project.name)}_tasks_${timestamp}.xlsx`;
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = fileName;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
