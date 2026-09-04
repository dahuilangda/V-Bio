import { describe, it, expect } from 'vitest';
import { renderToString } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { ProjectTasksHeader } from './ProjectTasksHeader';

type HeaderProps = Parameters<typeof ProjectTasksHeader>[0];

function headerProps(overrides: Partial<HeaderProps> = {}): HeaderProps {
  return {
    projectName: 'Docking',
    taskCountText: '12 tasks',
    refreshing: false,
    createTaskHref: '/create',
    backToCurrentTaskHref: '/back',
    canEdit: true,
    exportingExcel: false,
    exportProgress: null,
    filteredCount: 12,
    onDownloadExcel: () => {},
    onOpenApi: () => {},
    ...overrides
  };
}

function renderHeader(props: HeaderProps): string {
  return renderToString(
    <MemoryRouter>
      <ProjectTasksHeader {...props} />
    </MemoryRouter>
  );
}

describe('ProjectTasksHeader export progress UI', () => {
  it('renders no progress popover when idle', () => {
    const html = renderHeader(headerProps());
    expect(html).not.toContain('task-export-popover');
    expect(html).toContain('task-export-anchor'); // button wrapper is always present
    expect(html).toContain('title="Export task list"');
    expect(html).not.toContain('task-export-cancel-icon');
  });

  it('turns the button into a cancel control while exporting', () => {
    const html = renderHeader(
      headerProps({ exportingExcel: true, exportProgress: { phase: 'exporting', done: 10, total: 100 } })
    );
    expect(html).toContain('title="Cancel export"');
    expect(html).toContain('aria-label="Cancel export"');
    expect(html).toContain('task-export-cancel-icon');
    expect(html).toContain('task-export-progress-ring');
    // exporting keeps the button clickable (not disabled)
    expect(html).not.toContain('disabled=""');
  });

  it('shows indeterminate ring + popover while submitting', () => {
    const html = renderHeader(
      headerProps({ exportingExcel: true, exportProgress: { phase: 'submitting', done: 0, total: 840 } })
    );
    expect(html).toContain('task-export-anchor');
    expect(html).toContain('Exporting Excel');
    expect(html).toContain('Submitting 840 tasks');
    expect(html).toContain('task-export-popover-fill--pulse');
    expect(html.match(/task-export-progress-ring spin/g)?.length).toBe(1);
    expect(html).not.toContain('%</span>');
  });

  it('shows percentage, filled bar and counts while exporting', () => {
    const html = renderHeader(
      headerProps({ exportingExcel: true, exportProgress: { phase: 'exporting', done: 840, total: 840 } })
    );
    expect(html).toContain('>100%</span>');
    expect(html).toContain('width:100%');
    expect(html).toContain('840 / 840 tasks');
    expect(html).not.toContain('task-export-popover-fill--pulse');
    expect(html).toMatch(/stroke-dashoffset[:=]"?0/);
  });

  it('shows partial progress and download phase text', () => {
    const half = renderHeader(
      headerProps({ exportingExcel: true, exportProgress: { phase: 'exporting', done: 420, total: 840 } })
    );
    expect(half).toContain('>50%</span>');
    expect(half).toContain('width:50%');
    expect(half).toContain('420 / 840 tasks');

    const downloading = renderHeader(
      headerProps({ exportingExcel: true, exportProgress: { phase: 'downloading', done: 840, total: 840 } })
    );
    expect(downloading).toContain('Preparing download…');
  });

  it('formats large counts with locale separators', () => {
    const html = renderHeader(
      headerProps({ exportingExcel: true, exportProgress: { phase: 'exporting', done: 1234, total: 5678 } })
    );
    expect(html).toMatch(/1,234 \/ 5,678 tasks/);
  });

  it('shows the collecting phase while the full task list loads', () => {
    const partial = renderHeader(
      headerProps({ exportingExcel: true, exportProgress: { phase: 'collecting', done: 240, total: 13089 } })
    );
    expect(partial).toContain('Loading tasks');
    expect(partial).toContain('Loading tasks 240 / 13,089');
    expect(partial).toContain('>2%</span>');
    expect(partial).toContain('width:2%');

    const complete = renderHeader(
      headerProps({ exportingExcel: true, exportProgress: { phase: 'collecting', done: 13089, total: 13089 } })
    );
    expect(complete).toContain('Loading tasks 13,089 / 13,089');
    expect(complete).toContain('>100%</span>');
  });
});
