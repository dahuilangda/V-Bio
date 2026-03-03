import { useEffect, type MutableRefObject } from 'react';
import type { WorkspaceTab } from './workspaceTypes';

interface UseProjectRunUiEffectsInput {
  runRedirectTaskId: string | null;
  projectId: string | null;
  navigate: (to: string) => void;
  runRedirectTimerRef: MutableRefObject<number | null>;
  runSuccessNoticeTimerRef: MutableRefObject<number | null>;
  runMenuOpen: boolean;
  hasUnsavedChanges: boolean;
  submitting: boolean;
  saving: boolean;
  setRunMenuOpen: (open: boolean) => void;
  runActionRef: MutableRefObject<HTMLDivElement | null>;
  isPredictionWorkflow: boolean;
  isAffinityWorkflow: boolean;
  isLeadOptimizationWorkflow: boolean;
  workspaceTab: WorkspaceTab;
  topRunButtonRef: MutableRefObject<HTMLButtonElement | null>;
  setShowFloatingRunButton: (visible: boolean) => void;
}

export function useProjectRunUiEffects({
  runRedirectTaskId,
  projectId,
  navigate,
  runRedirectTimerRef,
  runSuccessNoticeTimerRef,
  runMenuOpen,
  hasUnsavedChanges,
  submitting,
  saving,
  setRunMenuOpen,
  runActionRef,
  isPredictionWorkflow,
  isAffinityWorkflow,
  isLeadOptimizationWorkflow,
  workspaceTab,
  topRunButtonRef,
  setShowFloatingRunButton
}: UseProjectRunUiEffectsInput): void {
  void isLeadOptimizationWorkflow;
  useEffect(() => {
    return () => {
      if (runRedirectTimerRef.current !== null) {
        window.clearTimeout(runRedirectTimerRef.current);
        runRedirectTimerRef.current = null;
      }
      if (runSuccessNoticeTimerRef.current !== null) {
        window.clearTimeout(runSuccessNoticeTimerRef.current);
        runSuccessNoticeTimerRef.current = null;
      }
    };
  }, [runRedirectTimerRef, runSuccessNoticeTimerRef]);

  useEffect(() => {
    if (!runRedirectTaskId || !projectId) return;
    const taskPagePath = `/projects/${projectId}/tasks`;
    if (runRedirectTimerRef.current !== null) {
      window.clearTimeout(runRedirectTimerRef.current);
      runRedirectTimerRef.current = null;
    }
    runRedirectTimerRef.current = window.setTimeout(() => {
      runRedirectTimerRef.current = null;
      navigate(taskPagePath);
      window.setTimeout(() => {
        if (window.location.pathname !== taskPagePath) {
          window.location.assign(taskPagePath);
        }
      }, 140);
    }, 620);
    return () => {
      if (runRedirectTimerRef.current !== null) {
        window.clearTimeout(runRedirectTimerRef.current);
        runRedirectTimerRef.current = null;
      }
    };
  }, [runRedirectTaskId, projectId, navigate, runRedirectTimerRef]);

  useEffect(() => {
    if (!runMenuOpen) return;
    if (hasUnsavedChanges && !submitting && !saving) return;
    setRunMenuOpen(false);
  }, [runMenuOpen, hasUnsavedChanges, submitting, saving, setRunMenuOpen]);

  useEffect(() => {
    if (!runMenuOpen) return;
    const onPointerDown = (event: globalThis.PointerEvent) => {
      if (!runActionRef.current) return;
      if (runActionRef.current.contains(event.target as Node)) return;
      setRunMenuOpen(false);
    };
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') {
        setRunMenuOpen(false);
      }
    };
    document.addEventListener('pointerdown', onPointerDown, true);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown, true);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [runMenuOpen, runActionRef, setRunMenuOpen]);

  useEffect(() => {
    const shouldEnableFloatingRun =
      (isPredictionWorkflow && (workspaceTab === 'components' || workspaceTab === 'constraints')) ||
      (isAffinityWorkflow && workspaceTab === 'components');
    if (!shouldEnableFloatingRun) {
      setShowFloatingRunButton(false);
      return;
    }

    const topRunButton = topRunButtonRef.current;
    if (!topRunButton) return;

    if (typeof IntersectionObserver === 'undefined') {
      const update = () => {
        setShowFloatingRunButton(window.scrollY > 260);
      };
      update();
      window.addEventListener('scroll', update, { passive: true });
      window.addEventListener('resize', update);
      return () => {
        window.removeEventListener('scroll', update);
        window.removeEventListener('resize', update);
      };
    }

    const observer = new IntersectionObserver(
      (entries) => {
        const entry = entries[0];
        const visible = Boolean(entry?.isIntersecting && entry.intersectionRatio > 0.92);
        setShowFloatingRunButton(!visible);
      },
      {
        threshold: [0, 0.5, 0.92, 1],
        rootMargin: '-64px 0px 0px 0px'
      }
    );
    observer.observe(topRunButton);
    return () => {
      observer.disconnect();
    };
  }, [isPredictionWorkflow, isAffinityWorkflow, isLeadOptimizationWorkflow, workspaceTab, topRunButtonRef, setShowFloatingRunButton]);
}
