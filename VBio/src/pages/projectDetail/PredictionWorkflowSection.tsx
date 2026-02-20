import type { CSSProperties, KeyboardEvent, PointerEvent, RefObject } from 'react';
import { ComponentInputEditor } from '../../components/project/ComponentInputEditor';
import type { MolstarResiduePick } from '../../components/project/MolstarViewer';
import { MolstarViewer } from '../../components/project/MolstarViewer';
import type { InputComponent, ProteinTemplateUpload } from '../../types/models';
import { PredictionComponentsSidebar, type PredictionComponentsSidebarProps } from './PredictionComponentsSidebar';
import { PredictionConstraintsWorkspace, type PredictionConstraintsWorkspaceProps } from './PredictionConstraintsWorkspace';

export type PredictionWorkspaceTab = 'results' | 'basics' | 'components' | 'constraints';

export interface PredictionWorkflowSectionProps {
  visible: boolean;
  workspaceTab: PredictionWorkspaceTab;
  canEdit: boolean;
  componentsWorkspaceRef: RefObject<HTMLDivElement | null>;
  isComponentsResizing: boolean;
  componentsGridStyle: CSSProperties;
  onComponentsResizerPointerDown: (event: PointerEvent<HTMLDivElement>) => void;
  onComponentsResizerKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void;
  components: InputComponent[];
  onComponentsChange: (components: InputComponent[]) => void;
  proteinTemplates: Record<string, ProteinTemplateUpload>;
  onProteinTemplateChange: (componentId: string, upload: ProteinTemplateUpload | null) => void;
  activeComponentId: string | null;
  onActiveComponentIdChange: (id: string | null) => void;
  onProteinTemplateResiduePick: (pick: MolstarResiduePick) => void;
  constraintsWorkspaceProps: Omit<PredictionConstraintsWorkspaceProps, 'visible'>;
  componentsSidebarProps: Omit<PredictionComponentsSidebarProps, 'visible'>;
}

export function PredictionWorkflowSection({
  visible,
  workspaceTab,
  canEdit,
  componentsWorkspaceRef,
  isComponentsResizing,
  componentsGridStyle,
  onComponentsResizerPointerDown,
  onComponentsResizerKeyDown,
  components,
  onComponentsChange,
  proteinTemplates,
  onProteinTemplateChange,
  activeComponentId,
  onActiveComponentIdChange,
  onProteinTemplateResiduePick,
  constraintsWorkspaceProps,
  componentsSidebarProps
}: PredictionWorkflowSectionProps) {
  if (!visible || workspaceTab === 'basics' || workspaceTab === 'results') return null;

  return (
    <div
      ref={workspaceTab === 'components' ? (componentsWorkspaceRef as RefObject<HTMLDivElement>) : null}
      className={`inputs-workspace ${workspaceTab === 'constraints' ? 'constraints-focus' : ''} ${
        workspaceTab === 'components' ? `components-resizable ${isComponentsResizing ? 'is-resizing' : ''}` : ''
      }`}
      style={workspaceTab === 'components' ? componentsGridStyle : undefined}
    >
      <div className="inputs-main">
        {workspaceTab === 'components' && (
          <ComponentInputEditor
            components={components}
            onChange={onComponentsChange}
            proteinTemplates={proteinTemplates}
            onProteinTemplateChange={onProteinTemplateChange}
            selectedComponentId={activeComponentId}
            onSelectedComponentIdChange={(id) => onActiveComponentIdChange(id)}
            showQuickAdd={false}
            compact
            renderProteinTemplateViewer={({ upload }) => (
              <section className="component-template-inline">
                <MolstarViewer
                  structureText={upload.content}
                  format={upload.format}
                  colorMode="white"
                  showSequence={false}
                  pickMode="alt-left"
                  onResiduePick={(pick: MolstarResiduePick) => onProteinTemplateResiduePick(pick)}
                />
              </section>
            )}
            disabled={!canEdit}
          />
        )}

        <PredictionConstraintsWorkspace visible={workspaceTab === 'constraints'} {...constraintsWorkspaceProps} />
      </div>

      {workspaceTab === 'components' && (
        <div
          className={`panel-resizer ${isComponentsResizing ? 'dragging' : ''}`}
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize components and workspace panels"
          tabIndex={0}
          onPointerDown={onComponentsResizerPointerDown}
          onKeyDown={onComponentsResizerKeyDown}
        />
      )}

      <PredictionComponentsSidebar visible={workspaceTab === 'components'} {...componentsSidebarProps} />
    </div>
  );
}
