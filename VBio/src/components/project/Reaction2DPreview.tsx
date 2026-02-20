import { memo, useEffect, useState } from 'react';
import { loadRDKitModule, type RDKitModule } from '../../utils/rdkit';

interface RDKitReaction {
  get_svg: (width: number, height: number) => string;
  set_new_coords?: () => void;
  delete: () => void;
}

interface Reaction2DPreviewProps {
  reactionSmarts: string;
  width?: number;
  height?: number;
}

function resolveReactionFactory(
  rdkit: RDKitModule
): ((reactionSmarts: string) => RDKitReaction | null) | null {
  const moduleAny = rdkit as RDKitModule & Record<string, unknown>;
  const candidates = ['get_rxn', 'get_rxn_from_smarts', 'get_reaction'] as const;
  for (const key of candidates) {
    const fn = moduleAny[key];
    if (typeof fn === 'function') {
      return fn as (reactionSmarts: string) => RDKitReaction | null;
    }
  }
  return null;
}

export function Reaction2DPreview({ reactionSmarts, width = 228, height = 132 }: Reaction2DPreviewProps) {
  const [svg, setSvg] = useState<string>('');
  const [error, setError] = useState<string>('');

  useEffect(() => {
    let cancelled = false;
    const value = reactionSmarts.trim();
    if (!value) {
      setSvg('');
      setError('');
      return () => {
        cancelled = true;
      };
    }

    const render = async () => {
      try {
        setError('');
        const rdkit = await loadRDKitModule();
        if (cancelled) return;
        const createReaction = resolveReactionFactory(rdkit);
        if (!createReaction) {
          throw new Error('RDKit reaction renderer is unavailable.');
        }
        const reaction = createReaction(value);
        if (!reaction) {
          throw new Error('Invalid reaction SMARTS.');
        }
        try {
          if (typeof reaction.set_new_coords === 'function') {
            reaction.set_new_coords();
          }
          const rendered = reaction.get_svg(width, height) || '';
          if (cancelled) return;
          setSvg(rendered);
        } finally {
          reaction.delete();
        }
      } catch (e) {
        if (cancelled) return;
        setSvg('');
        setError(e instanceof Error ? e.message : 'RDKit reaction render failed.');
      }
    };

    void render();
    return () => {
      cancelled = true;
    };
  }, [reactionSmarts, width, height]);

  if (!reactionSmarts.trim()) {
    return <div className="reaction-preview-empty">No transform.</div>;
  }
  if (error) {
    return <div className="reaction-preview-empty">Reaction preview unavailable.</div>;
  }
  if (!svg) {
    return <div className="reaction-preview-empty">Rendering transform...</div>;
  }
  return (
    <div className="reaction-preview-svg-wrap">
      <div className="reaction-preview-svg" dangerouslySetInnerHTML={{ __html: svg }} />
    </div>
  );
}

export const MemoReaction2DPreview = memo(Reaction2DPreview);
