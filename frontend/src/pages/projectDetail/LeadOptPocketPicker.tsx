import { useEffect, useMemo, useRef, useState } from 'react';
import { Box, RotateCcw } from 'lucide-react';
import { MolstarViewer } from '../../components/project/MolstarViewer';
import { PocketBoxControls } from '../../components/project/PocketBoxControls';
import { buildPocketBoxPdb, pocketTargetChanged, type PocketTargetSignature } from '../../utils/pocketBox';
import { pocketCenterString } from '../../utils/peptidePocket';
import type { AffinityDockPocket } from '../../types/models';

/**
 * Lead-optimization pocket picker on the uploaded TARGET structure — the
 * docking-style box (wireframe overlay + adjustment panel), remembered with
 * the draft/task snapshot via options.leadOptDockPocket. Clearing the box
 * leaves the pocket empty = blind docking.
 */
export function LeadOptPocketPicker({
  canEdit,
  targetStructure,
  dockPocket,
  onDockPocketChange,
  onPocketCenterChange
}: {
  canEdit: boolean;
  targetStructure: { fileName: string; format: 'pdb' | 'cif'; content: string } | null;
  dockPocket: AffinityDockPocket | null;
  onDockPocketChange: (pocket: AffinityDockPocket | null) => void;
  onPocketCenterChange: (center: string) => void;
}) {
  const hasStructure = Boolean(targetStructure && targetStructure.content.trim());
  const [boxWireframe, setBoxWireframe] = useState('');
  const [drawerOpen, setDrawerOpen] = useState(false);
  const lastTargetSignatureRef = useRef<{ name: string; length: number } | null>(null);

  const nextSignature: PocketTargetSignature | null = hasStructure && targetStructure
    ? { name: targetStructure.fileName, length: targetStructure.content.length }
    : null;

  useEffect(() => {
    // Only a real target change (switch/rebuild, not the empty→loaded arrival
    // of a restored task) invalidates the remembered box.
    const previous = lastTargetSignatureRef.current;
    lastTargetSignatureRef.current = nextSignature;
    if (!nextSignature || !pocketTargetChanged(previous, nextSignature)) return;
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

  const handleClear = () => {
    setDrawerOpen(false);
    setBoxWireframe('');
    onDockPocketChange(null);
    onPocketCenterChange('');
  };

  const viewerKey = useMemo(
    () => `lead-opt-pocket-${nextSignature?.name || 'none'}:${nextSignature?.length || 0}`,
    [nextSignature?.name, nextSignature?.length]
  );

  if (!hasStructure || !targetStructure) {
    return (
      <div className="field peptide-pocket-field peptide-pocket-field-component">
        <div className="peptide-pocket-head">
          <span className="peptide-pocket-title">
            Pocket box <span className="muted">(optional)</span>
          </span>
        </div>
        <div className="muted small">Upload a target structure to define a pocket box; without it the run is blind.</div>
      </div>
    );
  }

  return (
    <div className="field peptide-pocket-field peptide-pocket-field-component">
      <div className="peptide-pocket-head">
        <span className="peptide-pocket-title">
          Pocket box <span className="muted">(optional — clear = blind docking)</span>
        </span>
      </div>
      <div className="peptide-pocket-structure">
        <div className="peptide-pocket-toolbar">
          <button
            type="button"
            className={`btn pocket-box-btn ${drawerOpen ? 'active' : ''}`}
            onClick={() => setDrawerOpen((v) => !v)}
            disabled={!canEdit}
            title="Show the box controls (center/size sliders, ligand presets)"
          >
            <Box size={12} />
            {drawerOpen ? 'Hide box' : 'Box'}
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-compact"
            onClick={handleClear}
            disabled={!canEdit}
            title="Remove the box — optimization runs blind over the whole target"
          >
            <RotateCcw size={11} />
            Clear
          </button>
        </div>
        <div className="peptide-pocket-viewer">
          <MolstarViewer
            key={viewerKey}
            structureText={targetStructure.content}
            format={targetStructure.format}
            overlayStructureText={boxWireframe || undefined}
            overlayFormat="pdb"
            colorMode="default"
            pickMode="alt-left"
            showSequence={false}
          />
          {drawerOpen ? (
            <PocketBoxControls
              pocket={dockPocket}
              onPocketChange={onDockPocketChange}
              proteinStructureText={targetStructure.content}
              proteinStructureFormat={targetStructure.format}
              pickedResidues={[]}
              onBoxWireframeChange={setBoxWireframe}
              onCollapse={() => setDrawerOpen(false)}
              canEdit={canEdit}
              submitting={false}
            />
          ) : null}
        </div>
      </div>
    </div>
  );
}
