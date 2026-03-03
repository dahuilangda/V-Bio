export const ALPHAFOLD_COLOR_BINS = [
  { min: 90, color: '#0053D6', label: 'Very high (90-100)' },
  { min: 70, color: '#65CBF3', label: 'Confident (70-90)' },
  { min: 50, color: '#FFDB13', label: 'Low (50-70)' },
  { min: 0, color: '#FF7D45', label: 'Very low (0-50)' }
] as const;

export const alphafoldLegend = ALPHAFOLD_COLOR_BINS.map((i) => ({
  ...i,
  key: `${i.min}-${i.color}`
}));
