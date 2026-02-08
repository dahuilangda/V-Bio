import yaml from 'js-yaml';
import type { InputComponent, PredictionConstraint, PredictionProperties } from '../types/models';
import { assignChainIdsForComponents } from './chainAssignments';

export function buildPredictionYaml(proteinSequence: string, ligandSmiles: string): string {
  const payload = {
    version: 1,
    sequences: [
      {
        protein: {
          id: 'A',
          sequence: proteinSequence.replace(/\s+/g, '')
        }
      },
      {
        ligand: {
          id: 'B',
          smiles: ligandSmiles.trim()
        }
      }
    ]
  };

  return yaml.dump(payload, {
    lineWidth: 120,
    noRefs: true,
    sortKeys: false
  });
}

function normalizeId(chainIds: string[]): string | string[] {
  return chainIds.length === 1 ? chainIds[0] : chainIds;
}

function normalizePositiveInt(value: unknown, fallback = 1): number {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return fallback;
  return Math.max(1, Math.floor(numeric));
}

function normalizeElementSymbol(raw: string): string {
  if (!raw) return 'C';
  if (raw.length === 1) return raw.toUpperCase();
  return `${raw[0].toUpperCase()}${raw.slice(1).toLowerCase()}`;
}

function inferLigandToken(sequence: string): string {
  const trimmed = sequence.trim();
  const monoAtomMatch = trimmed.match(/^\[([A-Za-z]{1,2})\]$/);
  if (monoAtomMatch) {
    return normalizeElementSymbol(monoAtomMatch[1]);
  }

  const bracketed = trimmed.match(/\[([A-Za-z]{1,2})/);
  if (bracketed) {
    return `${normalizeElementSymbol(bracketed[1])}1`;
  }

  const token = trimmed.match(/Br|Cl|[BCNOSPFIbcnohspf]/)?.[0] || '';
  const symbol = token ? normalizeElementSymbol(token) : 'C';
  return `${symbol}1`;
}

function buildConstraintPayload(
  constraints: PredictionConstraint[],
  chainTypeById: Map<string, InputComponent['type']>,
  ligandTokenById: Map<string, string | number>,
  chainIds: string[]
): Record<string, unknown>[] {
  const validChainIds = new Set(chainIds);
  const fallbackChain = chainIds[0] || 'A';
  const fallbackDistinctChain = chainIds.find((id) => id !== fallbackChain) || fallbackChain;
  const fallbackLigandChain = chainIds.find((id) => chainTypeById.get(id) === 'ligand') || fallbackChain;

  const normalizeChain = (candidate: string, fallback: string): string =>
    validChainIds.has(candidate) ? candidate : fallback;

  const normalizeToken = (chainId: string, residueLike: unknown): string | number => {
    if (chainTypeById.get(chainId) === 'ligand') {
      return ligandTokenById.get(chainId) ?? 'C1';
    }
    return normalizePositiveInt(residueLike, 1);
  };

  const payloads: Array<Record<string, unknown> | null> = constraints.map((constraint) => {
      if (constraint.type === 'contact') {
        const token1Chain = normalizeChain(constraint.token1_chain, fallbackChain);
        const token2Chain = normalizeChain(constraint.token2_chain, fallbackDistinctChain);
        const token1Type = chainTypeById.get(token1Chain);
        const token2Type = chainTypeById.get(token2Chain);

        // Ligand+polymer contact is normalized to pocket constraint to avoid invalid ligand residue indices.
        if (token1Type === 'ligand' && token2Type !== 'ligand') {
          return {
            pocket: {
              binder: token1Chain,
              contacts: [[token2Chain, normalizeToken(token2Chain, constraint.token2_residue)]],
              max_distance: Math.max(1, Number(constraint.max_distance || 5)),
              force: Boolean(constraint.force)
            }
          };
        }
        if (token2Type === 'ligand' && token1Type !== 'ligand') {
          return {
            pocket: {
              binder: token2Chain,
              contacts: [[token1Chain, normalizeToken(token1Chain, constraint.token1_residue)]],
              max_distance: Math.max(1, Number(constraint.max_distance || 5)),
              force: Boolean(constraint.force)
            }
          };
        }

        return {
          contact: {
            token1: [token1Chain, normalizeToken(token1Chain, constraint.token1_residue)],
            token2: [token2Chain, normalizeToken(token2Chain, constraint.token2_residue)],
            max_distance: Math.max(1, Number(constraint.max_distance || 5)),
            force: Boolean(constraint.force)
          }
        };
      }

      if (constraint.type === 'bond') {
        const atom1Chain = normalizeChain(constraint.atom1_chain, fallbackChain);
        const atom2Chain = normalizeChain(constraint.atom2_chain, fallbackDistinctChain);
        const atom1Type = chainTypeById.get(atom1Chain);
        const atom2Type = chainTypeById.get(atom2Chain);
        const atom1Default = String(constraint.atom1_atom || '').trim().toUpperCase();
        const atom2Default = String(constraint.atom2_atom || '').trim().toUpperCase();
        const atom1Name =
          atom1Type === 'ligand' && (!atom1Default || atom1Default === 'CA')
            ? String(ligandTokenById.get(atom1Chain) || 'C1')
            : (constraint.atom1_atom || 'CA').trim();
        const atom2Name =
          atom2Type === 'ligand' && (!atom2Default || atom2Default === 'CA')
            ? String(ligandTokenById.get(atom2Chain) || 'C1')
            : (constraint.atom2_atom || 'CA').trim();
        return {
          bond: {
            atom1: [
              atom1Chain,
              normalizePositiveInt(constraint.atom1_residue, 1),
              atom1Name
            ],
            atom2: [
              atom2Chain,
              normalizePositiveInt(constraint.atom2_residue, 1),
              atom2Name
            ]
          }
        };
      }

      const contacts: Array<[string, string | number]> = (constraint.contacts || []).reduce<Array<[string, string | number]>>(
        (acc, item) => {
          if (!Array.isArray(item) || item.length < 2 || typeof item[0] !== 'string') return acc;
          const chainId = normalizeChain(item[0], fallbackChain);
          const token = normalizeToken(chainId, item[1]);
          acc.push([chainId, token]);
          return acc;
        },
        []
      );

      if (contacts.length === 0) return null;
      const binder = normalizeChain(constraint.binder, fallbackLigandChain);

      return {
        pocket: {
          binder,
          contacts,
          max_distance: Math.max(1, Number(constraint.max_distance || 6)),
          force: Boolean(constraint.force)
        }
      };
    });

  return payloads.filter((item): item is Record<string, unknown> => item !== null);
}

function buildPropertyPayload(
  properties: PredictionProperties | undefined,
  chainTypeById: Map<string, InputComponent['type']>,
  chainIds: string[]
): Array<Record<string, unknown>> {
  if (!properties?.affinity) return [];
  const validChainIds = new Set(chainIds);
  const binderCandidate = String(properties.ligand || properties.binder || '').trim();
  if (!binderCandidate || !validChainIds.has(binderCandidate)) return [];
  if (chainTypeById.get(binderCandidate) !== 'ligand') return [];

  const affinity: Record<string, unknown> = { binder: binderCandidate };
  const targetCandidate = String(properties.target || '').trim();
  if (targetCandidate && validChainIds.has(targetCandidate) && chainTypeById.get(targetCandidate) !== 'ligand') {
    affinity.target = targetCandidate;
  }
  return [{ affinity }];
}

interface BuildYamlOptions {
  constraints?: PredictionConstraint[];
  properties?: PredictionProperties;
}

export function buildPredictionYamlFromComponents(components: InputComponent[], options: BuildYamlOptions = {}): string {
  const assignments = assignChainIdsForComponents(components);
  const chainIds = assignments.flat();
  const chainTypeById = new Map<string, InputComponent['type']>();
  const ligandTokenById = new Map<string, string | number>();

  components.forEach((comp, index) => {
    const ids = assignments[index] || [];
    for (const id of ids) {
      chainTypeById.set(id, comp.type);
      if (comp.type === 'ligand') {
        ligandTokenById.set(id, inferLigandToken(comp.sequence));
      }
    }
  });

  const sequences = components.map((comp, idx) => {
    const chainIds = assignments[idx];
    const idValue = normalizeId(chainIds);

    if (comp.type === 'ligand') {
      const inputMethod = comp.inputMethod === 'ccd' ? 'ccd' : 'smiles';
      return {
        ligand: {
          id: idValue,
          [inputMethod]: comp.sequence.trim()
        }
      };
    }

    const core: Record<string, unknown> = {
      id: idValue,
      sequence: comp.sequence.replace(/\s+/g, '')
    };

    if (comp.type === 'protein') {
      if (comp.cyclic) core.cyclic = true;
      if (comp.useMsa === false) core.msa = 'empty';
    }

    return {
      [comp.type]: core
    };
  });

  const payload: Record<string, unknown> = {
    version: 1,
    sequences
  };
  const propertyPayload = buildPropertyPayload(options.properties, chainTypeById, chainIds);
  if (propertyPayload.length > 0) {
    payload.properties = propertyPayload;
  }
  const constraints = buildConstraintPayload(options.constraints || [], chainTypeById, ligandTokenById, chainIds);
  if (constraints.length > 0) {
    payload.constraints = constraints;
  }

  return yaml.dump(payload, {
    lineWidth: 120,
    noRefs: true,
    sortKeys: false
  });
}
