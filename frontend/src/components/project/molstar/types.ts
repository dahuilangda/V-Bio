export interface MolstarResiduePick {
  chainId: string;
  residue: number;
  atomName?: string;
  label: string;
}

export interface MolstarResidueHighlight {
  chainId: string;
  residue: number;
  emphasis?: 'default' | 'active';
}

export interface MolstarAtomHighlight {
  chainId: string;
  residue: number;
  atomName: string;
  atomIndex?: number;
  emphasis?: 'default' | 'active';
}
