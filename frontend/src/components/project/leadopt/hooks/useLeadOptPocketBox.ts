import { useEffect, useRef, useState } from 'react';
import {
  buildPocketBoxPdb,
  computePocketBoxFromLigandAtoms,
  parseStructureAtomCoords,
  pocketTargetChanged,
  type PocketTargetSignature
} from '../../../../utils/pocketBox';
import { pocketCenterString } from '../../../../utils/peptidePocket';
import type { AffinityDockPocket } from '../../../../types/models';

interface LeadOptPocketTarget {
  fileName: string;
  content: string;
}

// Mirror PocketBoxControls' slider clamp so the auto box matches what the
// user can set by hand.
const MAX_BOX_SIZE = 40;

/**
 * Docking-style pocket box state for the lead-opt reference viewer. Once the
 * combined reference preview is ready the box is created automatically around
 * the REFERENCE LIGAND (coordinates parsed from the ligand overlay structure —
 * the main preview text is target-only). No dedicated pocket step; the user
 * adjusts the box from there (or clears it for blind docking). A box the user
 * moved (method != 'ligand') always wins. Remembered with the draft/task
 * snapshot via options.leadOptDockPocket; only a real target change invalidates
 * it.
 */
export function useLeadOptPocketBox({
  targetStructure,
  referenceOverlayText,
  referenceOverlayFormat,
  referenceReady,
  dockPocket,
  onDockPocketChange,
  onPocketCenterChange
}: {
  targetStructure: LeadOptPocketTarget | null;
  referenceOverlayText: string;
  referenceOverlayFormat: 'cif' | 'pdb';
  referenceReady: boolean;
  dockPocket: AffinityDockPocket | null;
  onDockPocketChange: (pocket: AffinityDockPocket | null) => void;
  onPocketCenterChange: (center: string) => void;
}) {
  const hasStructure = Boolean(targetStructure && targetStructure.content.trim());
  const [boxWireframe, setBoxWireframe] = useState('');
  const [drawerOpen, setDrawerOpen] = useState(false);
  const lastTargetSignatureRef = useRef<{ name: string; length: number } | null>(null);
  const autoBoxSignatureRef = useRef('');

  const nextSignature: PocketTargetSignature | null = hasStructure && targetStructure
    ? { name: targetStructure.fileName, length: targetStructure.content.length }
    : null;

  useEffect(() => {
    const previous = lastTargetSignatureRef.current;
    lastTargetSignatureRef.current = nextSignature;
    if (!nextSignature || !pocketTargetChanged(previous, nextSignature)) return;
    autoBoxSignatureRef.current = '';
    setBoxWireframe('');
    onDockPocketChange(null);
    onPocketCenterChange('');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nextSignature?.name, nextSignature?.length]);

  useEffect(() => {
    if (dockPocket && nextSignature) {
      setBoxWireframe(buildPocketBoxPdb(dockPocket));
      onPocketCenterChange(pocketCenterString(dockPocket));
    } else if (!dockPocket && !nextSignature) {
      setBoxWireframe('');
      onPocketCenterChange('');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dockPocket?.centerX, dockPocket?.centerY, dockPocket?.centerZ, dockPocket?.sizeX, dockPocket?.sizeY, dockPocket?.sizeZ]);

  useEffect(() => {
    if (!referenceReady) return;
    const overlayText = String(referenceOverlayText || '').trim();
    if (!overlayText) return;
    const signature = `${overlayText.length}:${referenceOverlayFormat}`;
    if (autoBoxSignatureRef.current === signature) return;
    if (dockPocket && dockPocket.method !== 'ligand') return;
    autoBoxSignatureRef.current = signature;
    const coords = parseStructureAtomCoords(overlayText, referenceOverlayFormat);
    if (coords.length === 0) return;
    const box = computePocketBoxFromLigandAtoms({
      chainId: '',
      resName: 'LIG',
      resNum: 1,
      coords: coords.map((atom) => [atom.x, atom.y, atom.z] as [number, number, number]),
      atomCount: coords.length,
      label: 'LIG'
    });
    if (!box) return;
    onDockPocketChange({
      centerX: Math.round(box.centerX),
      centerY: Math.round(box.centerY),
      centerZ: Math.round(box.centerZ),
      sizeX: Math.min(MAX_BOX_SIZE, Math.round(box.sizeX)),
      sizeY: Math.min(MAX_BOX_SIZE, Math.round(box.sizeY)),
      sizeZ: Math.min(MAX_BOX_SIZE, Math.round(box.sizeZ)),
      method: 'ligand'
    });
  }, [
    dockPocket,
    onDockPocketChange,
    referenceOverlayFormat,
    referenceOverlayText,
    referenceReady
  ]);

  const toggleDrawer = () => setDrawerOpen((v) => !v);

  const clearPocket = () => {
    setDrawerOpen(false);
    setBoxWireframe('');
    onDockPocketChange(null);
    onPocketCenterChange('');
  };

  return {
    hasTarget: hasStructure,
    boxWireframe,
    drawerOpen,
    toggleDrawer,
    clearPocket,
    applyBoxWireframe: setBoxWireframe
  };
}
