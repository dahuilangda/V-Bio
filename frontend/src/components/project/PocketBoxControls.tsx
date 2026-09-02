import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Minus, Plus, RotateCcw, X } from 'lucide-react';
import { buildPocketBoxPdb, computePocketBoxFromResiduePicks, detectLigands, computePocketBoxFromLigandAtoms, type DetectedLigand } from '../../utils/pocketBox';
import type { MolstarResiduePick } from './MolstarViewer';

export interface DockPocket {
  centerX: number;
  centerY: number;
  centerZ: number;
  sizeX: number;
  sizeY: number;
  sizeZ: number;
  method: 'residues' | 'manual' | 'ligand';
}

interface PocketBoxControlsProps {
  pocket: DockPocket | null;
  onPocketChange: (pocket: DockPocket | null) => void;
  proteinStructureText: string;
  proteinStructureFormat: 'cif' | 'pdb';
  pickedResidues: MolstarResiduePick[];
  onBoxWireframeChange: (pdb: string) => void;
  onCollapse: () => void;
  canEdit: boolean;
  submitting: boolean;
}

const SIZE_STEP = 2;
const CENTER_STEP = 1;
const MIN_SIZE = 10;
const MAX_SIZE = 40;

/** Compute CA bounds once from protein for stable slider ranges. */
function useProteinBounds(text: string, format: 'cif' | 'pdb') {
  return useMemo(() => {
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity, minZ = Infinity, maxZ = -Infinity, n = 0;
    const consider = (x: number, y: number, z: number) => {
      if (Number.isFinite(x) && Number.isFinite(y) && Number.isFinite(z)) {
        minX = Math.min(minX, x); maxX = Math.max(maxX, x);
        minY = Math.min(minY, y); maxY = Math.max(maxY, y);
        minZ = Math.min(minZ, z); maxZ = Math.max(maxZ, z);
        n++;
      }
    };
    if (format === 'pdb') {
      for (const line of text.split('\n')) {
        if (!line.startsWith('ATOM')) continue;
        if (line.slice(12, 16).trim() !== 'CA') continue;
        consider(parseFloat(line.slice(30, 38)), parseFloat(line.slice(38, 46)), parseFloat(line.slice(46, 54)));
      }
    } else {
      const lines = text.split('\n');
      let fields: string[] = [];
      let inAtomSite = false;
      for (const line of lines) {
        if (line.startsWith('#')) continue;
        if (line.trimStart().startsWith('_atom_site.')) {
          if (!inAtomSite) { inAtomSite = true; fields = []; }
          fields.push(line.trim().split('.')[1]);
          continue;
        }
        if (inAtomSite && (line.startsWith('ATOM') || line.startsWith('HETATM'))) {
          const parts = line.trim().split(/\s+/);
          const get = (name: string): string => {
            const idx = fields.indexOf(name);
            return idx >= 0 ? parts[idx] : '';
          };
          if (get('label_atom_id').toUpperCase() !== 'CA') continue;
          consider(parseFloat(get('Cartn_x')), parseFloat(get('Cartn_y')), parseFloat(get('Cartn_z')));
        } else if (inAtomSite && fields.length > 0 && (line.trim() === '' || line.trimStart().startsWith('_') || line.trimStart().startsWith('loop_'))) {
          if (n > 0) inAtomSite = false;
        }
      }
    }
    if (n === 0) return null;
    return {
      centerX: (minX + maxX) / 2, centerY: (minY + maxY) / 2, centerZ: (minZ + maxZ) / 2,
      minX: Math.floor(minX) - 5, maxX: Math.ceil(maxX) + 5,
      minY: Math.floor(minY) - 5, maxY: Math.ceil(maxY) + 5,
      minZ: Math.floor(minZ) - 5, maxZ: Math.ceil(maxZ) + 5,
    };
  }, [text, format]);
}

export function PocketBoxControls({
  pocket,
  onPocketChange,
  proteinStructureText,
  proteinStructureFormat,
  pickedResidues,
  onBoxWireframeChange,
  onCollapse,
  canEdit,
  submitting
}: PocketBoxControlsProps) {
  const disabled = !canEdit || submitting;
  const bounds = useProteinBounds(proteinStructureText, proteinStructureFormat);
  const detectedLigands = useMemo(
    () => detectLigands(proteinStructureText, proteinStructureFormat),
    [proteinStructureText, proteinStructureFormat]
  );
  const wireframeTimerRef = useRef<number | null>(null);
  const initialCenterRef = useRef<DockPocket | null>(null);
  const autoLigandRef = useRef<DetectedLigand | null>(null);

  // Drag state for floating panel
  const panelRef = useRef<HTMLDivElement | null>(null);
  const [pos, setPos] = useState({ x: 0, y: 0 });
  const dragRef = useRef<{ startX: number; startY: number; baseX: number; baseY: number } | null>(null);

  const handleDragStart = useCallback((e: React.PointerEvent) => {
    if ((e.target as HTMLElement).closest('button, input')) return;
    dragRef.current = { startX: e.clientX, startY: e.clientY, baseX: pos.x, baseY: pos.y };
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
  }, [pos]);

  const handleDragMove = useCallback((e: React.PointerEvent) => {
    if (!dragRef.current) return;
    const dx = e.clientX - dragRef.current.startX;
    const dy = e.clientY - dragRef.current.startY;
    setPos({ x: dragRef.current.baseX + dx, y: dragRef.current.baseY + dy });
  }, []);

  const handleDragEnd = useCallback(() => {
    dragRef.current = null;
  }, []);

  // Auto-initialize pocket at first detected ligand, else protein center
  useEffect(() => {
    if (!bounds) return;
    const defaults: DockPocket = {
      centerX: Math.round(bounds.centerX),
      centerY: Math.round(bounds.centerY),
      centerZ: Math.round(bounds.centerZ),
      sizeX: 22, sizeY: 22, sizeZ: 22,
      method: 'manual'
    };
    if (!initialCenterRef.current) {
      initialCenterRef.current = defaults;
    }
    if (!pocket) {
      const first = detectedLigands[0];
      if (first) {
        autoLigandRef.current = first;
        const lb = computePocketBoxFromLigandAtoms(first);
        if (lb) {
          onPocketChange({
            centerX: Math.round(lb.centerX), centerY: Math.round(lb.centerY), centerZ: Math.round(lb.centerZ),
            sizeX: Math.min(MAX_SIZE, Math.round(lb.sizeX)),
            sizeY: Math.min(MAX_SIZE, Math.round(lb.sizeY)),
            sizeZ: Math.min(MAX_SIZE, Math.round(lb.sizeZ)),
            method: 'ligand'
          });
          return;
        }
      }
      onPocketChange(defaults);
    }
  }, [pocket, bounds, detectedLigands, onPocketChange]);

  // Recompute from picked residues
  useEffect(() => {
    if (pickedResidues.length === 0) return;
    const box = computePocketBoxFromResiduePicks(proteinStructureText, proteinStructureFormat, pickedResidues);
    if (box) {
      onPocketChange({
        centerX: Math.round(box.centerX),
        centerY: Math.round(box.centerY),
        centerZ: Math.round(box.centerZ),
        sizeX: Math.min(MAX_SIZE, Math.round(box.sizeX)),
        sizeY: Math.min(MAX_SIZE, Math.round(box.sizeY)),
        sizeZ: Math.min(MAX_SIZE, Math.round(box.sizeZ)),
        method: 'residues'
      });
    }
  }, [pickedResidues, proteinStructureText, proteinStructureFormat, onPocketChange]);

  // Debounced wireframe generation
  useEffect(() => {
    if (wireframeTimerRef.current !== null) {
      window.clearTimeout(wireframeTimerRef.current);
    }
    wireframeTimerRef.current = window.setTimeout(() => {
      onBoxWireframeChange(pocket ? buildPocketBoxPdb(pocket) : '');
      wireframeTimerRef.current = null;
    }, 250);
    return () => {
      if (wireframeTimerRef.current !== null) {
        window.clearTimeout(wireframeTimerRef.current);
      }
    };
  }, [pocket, onBoxWireframeChange]);

  const adjust = useCallback(
    (axis: 'centerX' | 'centerY' | 'centerZ' | 'sizeX' | 'sizeY' | 'sizeZ', delta: number) => {
      if (!pocket) return;
      let v = (pocket[axis] as number) + delta;
      if (axis.startsWith('size')) {
        v = Math.max(MIN_SIZE, Math.min(MAX_SIZE, v));
      } else if (bounds) {
        const min = (bounds as any)[axis.replace('center', 'min')] ?? -Infinity;
        const max = (bounds as any)[axis.replace('center', 'max')] ?? Infinity;
        v = Math.max(min, Math.min(max, v));
      }
      onPocketChange({ ...pocket, [axis]: v, method: 'manual' });
    },
    [pocket, bounds, onPocketChange]
  );

  const setAxis = useCallback(
    (axis: 'centerX' | 'centerY' | 'centerZ' | 'sizeX' | 'sizeY' | 'sizeZ', value: number) => {
      if (!pocket || !Number.isFinite(value)) return;
      let v = value;
      if (axis.startsWith('size')) v = Math.max(MIN_SIZE, Math.min(MAX_SIZE, v));
      onPocketChange({ ...pocket, [axis]: v, method: 'manual' });
    },
    [pocket, onPocketChange]
  );

  const setAllSizes = useCallback((size: number) => {
    if (!pocket) return;
    onPocketChange({ ...pocket, sizeX: size, sizeY: size, sizeZ: size, method: 'manual' });
  }, [pocket, onPocketChange]);

  const resetBox = useCallback(() => {
    if (initialCenterRef.current) {
      onPocketChange({ ...initialCenterRef.current, method: 'manual' });
    }
  }, [onPocketChange]);

  const applyLigandPocket = useCallback((ligand: DetectedLigand) => {
    const box = computePocketBoxFromLigandAtoms(ligand);
    if (box) {
      autoLigandRef.current = ligand;
      onPocketChange({
        centerX: Math.round(box.centerX),
        centerY: Math.round(box.centerY),
        centerZ: Math.round(box.centerZ),
        sizeX: Math.min(MAX_SIZE, Math.round(box.sizeX)),
        sizeY: Math.min(MAX_SIZE, Math.round(box.sizeY)),
        sizeZ: Math.min(MAX_SIZE, Math.round(box.sizeZ)),
        method: 'ligand'
      });
    }
  }, [onPocketChange]);

  if (!pocket || !bounds) {
    return null;
  }

  const axes = [
    { label: 'X', cKey: 'centerX' as const, sKey: 'sizeX' as const, min: bounds.minX, max: bounds.maxX },
    { label: 'Y', cKey: 'centerY' as const, sKey: 'sizeY' as const, min: bounds.minY, max: bounds.maxY },
    { label: 'Z', cKey: 'centerZ' as const, sKey: 'sizeZ' as const, min: bounds.minZ, max: bounds.maxZ },
  ];

  // Check which ligand (if any) the current pocket matches
  const activeLigandKey = pocket.method === 'ligand'
    ? detectedLigands.find(l => {
        const lb = computePocketBoxFromLigandAtoms(l);
        return lb && Math.abs(Math.round(lb.centerX) - pocket.centerX) < 2;
      })?.resName
    : null;

  return (
    <div
      ref={panelRef}
      className="pocket-float-panel"
      style={pos.x !== 0 || pos.y !== 0 ? { transform: `translate(${pos.x}px, ${pos.y}px)` } : undefined}
    >
      <div
        className="pocket-float-header"
        onPointerDown={handleDragStart}
        onPointerMove={handleDragMove}
        onPointerUp={handleDragEnd}
        onPointerLeave={handleDragEnd}
      >
        <span className="pocket-float-title">
          {pocket.sizeX.toFixed(0)}×{pocket.sizeY.toFixed(0)}×{pocket.sizeZ.toFixed(0)} Å
        </span>
        <button type="button" className="pocket-btn" onClick={resetBox} disabled={disabled} title="Reset to protein center">
          <RotateCcw size={11} />
        </button>
        <button type="button" className="pocket-btn pocket-close-btn" onClick={onCollapse} title="Close">
          <X size={13} />
        </button>
      </div>

      {detectedLigands.length > 0 && (
        <div className="pocket-ligand-row">
          {detectedLigands.map((lig) => (
            <button
              key={`${lig.chainId}:${lig.resName}:${lig.resNum}`}
              type="button"
              className={`pocket-ligand-chip ${activeLigandKey === lig.resName ? 'active' : ''}`}
              onClick={() => applyLigandPocket(lig)}
              disabled={disabled}
              title={`${lig.atomCount} atoms — click to move box here`}
            >
              {lig.resName}
            </button>
          ))}
        </div>
      )}

      <div className="pocket-grid">
        {axes.map(({ label, cKey, sKey, min, max }) => (
          <div key={label} className="pocket-axis-row">
            <span className="pocket-axis-label">{label}</span>
            <div className="pocket-slider-group">
              <div className="pocket-slider-head">
                <span className="pocket-ctrl-label">{pocket[cKey].toFixed(1)}</span>
                <div className="pocket-ctrl">
                  <button type="button" className="pocket-btn" onClick={() => adjust(cKey, -CENTER_STEP)} disabled={disabled}><Minus size={10} /></button>
                  <button type="button" className="pocket-btn" onClick={() => adjust(cKey, CENTER_STEP)} disabled={disabled}><Plus size={10} /></button>
                </div>
              </div>
              <input
                type="range" min={min} max={max} step={1} value={pocket[cKey]}
                disabled={disabled}
                onChange={(e) => setAxis(cKey, parseFloat(e.target.value))}
                className="pocket-slider pocket-slider-center"
                aria-label={`Center ${label} axis`}
              />
            </div>
            <div className="pocket-slider-group">
              <div className="pocket-slider-head">
                <span className="pocket-ctrl-label">{pocket[sKey].toFixed(0)}</span>
                <div className="pocket-ctrl">
                  <button type="button" className="pocket-btn" onClick={() => adjust(sKey, -SIZE_STEP)} disabled={disabled}><Minus size={10} /></button>
                  <button type="button" className="pocket-btn" onClick={() => adjust(sKey, SIZE_STEP)} disabled={disabled}><Plus size={10} /></button>
                </div>
              </div>
              <input
                type="range" min={MIN_SIZE} max={MAX_SIZE} step={2} value={pocket[sKey]}
                disabled={disabled}
                onChange={(e) => setAxis(sKey, parseFloat(e.target.value))}
                className="pocket-slider pocket-slider-size"
                aria-label={`Size ${label} axis`}
              />
            </div>
          </div>
        ))}
      </div>

      <div className="pocket-presets">
        {[16, 22, 30].map((size) => (
          <button
            key={size}
            type="button"
            className={`btn btn-ghost pocket-preset-btn ${pocket.sizeX === size ? 'active' : ''}`}
            onClick={() => setAllSizes(size)}
            disabled={disabled}
          >
            {size}³
          </button>
        ))}
        <div className="pocket-size-all">
          <button type="button" className="pocket-btn" onClick={() => setAllSizes(Math.max(MIN_SIZE, pocket.sizeX - SIZE_STEP))} disabled={disabled}><Minus size={10} /></button>
          <button type="button" className="pocket-btn" onClick={() => setAllSizes(Math.min(MAX_SIZE, pocket.sizeX + SIZE_STEP))} disabled={disabled}><Plus size={10} /></button>
        </div>
      </div>

      {pickedResidues.length > 0 && (
        <div className="affinity-pocket-picks">
          {pickedResidues.slice(0, 6).map((p) => (
            <span key={`${p.chainId}:${p.residue}`} className="affinity-pocket-chip">
              {p.label || `${p.chainId}:${p.residue}`}
            </span>
          ))}
          {pickedResidues.length > 6 && <span className="muted small">+{pickedResidues.length - 6}</span>}
        </div>
      )}
    </div>
  );
}
