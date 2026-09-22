import type { CSSProperties, KeyboardEvent, PointerEvent, RefObject } from 'react';
import { ComponentInputEditor } from '../../components/project/ComponentInputEditor';
import type { MolstarResiduePick } from '../../components/project/MolstarViewer';
import { MolstarViewer } from '../../components/project/MolstarViewer';
import type { CustomCcdMoleculeInput, InputComponent, ProteinTemplateUpload } from '../../types/models';
import { PeptidePocketPicker, type PeptideTargetPocketContext } from './PeptidePocketPicker';
import { PredictionComponentsSidebar, type PredictionComponentsSidebarProps } from './PredictionComponentsSidebar';
import { PredictionConstraintsWorkspace, type PredictionConstraintsWorkspaceProps } from './PredictionConstraintsWorkspace';

export type PredictionWorkspaceTab = 'results' | 'basics' | 'components' | 'constraints';

export interface PredictionWorkflowSectionProps {
  isVisible: boolean;
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
  isProteinMsaAllowed: boolean;
  isProteinTemplatesAllowed: boolean;
  customResidueLibrary: CustomCcdMoleculeInput[];
  onCustomResidueLibraryChange: (library: CustomCcdMoleculeInput[]) => void;
  onProteinTemplateChange: (componentId: string, upload: ProteinTemplateUpload | null) => void;
  activeComponentId: string | null;
  onActiveComponentIdChange: (id: string | null) => void;
  onProteinTemplateResiduePick: (pick: MolstarResiduePick) => void;
  constraintsWorkspaceProps: Omit<PredictionConstraintsWorkspaceProps, 'isVisible'>;
  componentsSidebarProps: Omit<PredictionComponentsSidebarProps, 'isVisible'>;
  /** Peptide design only: pocket state for the Binding target component. */
  peptideTargetPocket?: PeptideTargetPocketContext | null;
}

export function PredictionWorkflowSection({
  isVisible,
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
  isProteinMsaAllowed,
  isProteinTemplatesAllowed,
  customResidueLibrary,
  onCustomResidueLibraryChange,
  onProteinTemplateChange,
  activeComponentId,
  onActiveComponentIdChange,
  onProteinTemplateResiduePick,
  constraintsWorkspaceProps,
  componentsSidebarProps,
  peptideTargetPocket = null
}: PredictionWorkflowSectionProps) {
  if (!isVisible || workspaceTab === 'basics' || workspaceTab === 'results') return null;

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
            isProteinMsaAllowed={isProteinMsaAllowed}
            isProteinTemplatesAllowed={isProteinTemplatesAllowed}
            customResidueLibrary={customResidueLibrary}
            onCustomResidueLibraryChange={onCustomResidueLibraryChange}
            onProteinTemplateChange={onProteinTemplateChange}
            selectedComponentId={activeComponentId}
            onSelectedComponentIdChange={(id) => onActiveComponentIdChange(id)}
            isQuickAddVisible={false}
            isCompact
            targetPocketComponentId={peptideTargetPocket ? peptideTargetPocket.componentId : null}
            renderTargetPocketPanel={
              peptideTargetPocket
                ? ({ upload }) => (
                    <PeptidePocketPicker
                      isEditable={canEdit}
                      targetComponentId={peptideTargetPocket.componentId}
                      targetTemplate={
                        upload
                          ? {
                              fileName: upload.fileName,
                              format: upload.format,
                              content: upload.content,
                              chainId: upload.chainId
                            }
                          : null
                      }
                      targetChainId={peptideTargetPocket.chainId}
                      targetSequence={peptideTargetPocket.sequence}
                      pocketCenter={peptideTargetPocket.pocketCenter}
                      pocketResidues={peptideTargetPocket.pocketResidues}
                      pocketBox={peptideTargetPocket.pocketBox}
                      dockPocket={peptideTargetPocket.dockPocket}
                      onPocketFieldChange={peptideTargetPocket.onPocketFieldChange}
                      onDockPocketChange={peptideTargetPocket.onDockPocketChange}
                    />
                  )
                : undefined
            }
            renderProteinTemplateViewer={({ upload }) => (
              <section className="component-template-inline">
                <MolstarViewer
                  structureText={upload.content}
                  format={upload.format}
                  colorMode="default"
                  isSequenceVisible={false}
                  pickMode="alt-left"
                  onResiduePick={(pick: MolstarResiduePick) => onProteinTemplateResiduePick(pick)}
                />
              </section>
            )}
            isDisabled={!canEdit}
          />
        )}

        {workspaceTab === 'constraints' ? <PredictionConstraintsWorkspace isVisible {...constraintsWorkspaceProps} /> : null}
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

      {workspaceTab === 'components' ? <PredictionComponentsSidebar isVisible {...componentsSidebarProps} /> : null}
    </div>
  );
}
