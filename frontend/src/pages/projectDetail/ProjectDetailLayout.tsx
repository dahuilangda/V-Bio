import { ProjectHeaderActions, type ProjectHeaderActionsProps } from './ProjectHeaderActions';
import { ProjectHeaderMeta, type ProjectHeaderMetaProps } from './ProjectHeaderMeta';
import { ProjectWorkspaceContent, type ProjectWorkspaceContentProps } from './ProjectWorkspaceContent';
import { RunFeedbackOverlays, type RunFeedbackOverlaysProps } from './RunFeedbackOverlays';
import { WorkspaceStepper, type WorkspaceStepperProps } from './WorkspaceStepper';

/**
 * Composition shell for the project-detail page. Each region is configured
 * through its child's own props group — this file deliberately declares no
 * flat props (previously 61 drilled through a single interface, of which 58
 * were verbatim forwards).
 */
export interface ProjectDetailLayoutProps {
  headerMetaProps: ProjectHeaderMetaProps;
  headerActionsProps: ProjectHeaderActionsProps;
  overlaysProps: RunFeedbackOverlaysProps;
  stepperProps: WorkspaceStepperProps;
  contentProps: ProjectWorkspaceContentProps;
}

export function ProjectDetailLayout({
  headerMetaProps,
  headerActionsProps,
  overlaysProps,
  stepperProps,
  contentProps
}: ProjectDetailLayoutProps) {
  return (
    <div className="page-grid project-detail">
      <section className="page-header">
        <ProjectHeaderMeta {...headerMetaProps} />
        <ProjectHeaderActions {...headerActionsProps} />
      </section>

      <RunFeedbackOverlays {...overlaysProps} />

      <div className="workspace-shell">
        <WorkspaceStepper {...stepperProps} />
        <ProjectWorkspaceContent {...contentProps} />
      </div>
    </div>
  );
}
