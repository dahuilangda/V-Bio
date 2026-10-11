"""Unit tests for the peptide archive layout helpers."""

import unittest

from backend.runtime.peptide_archive_layout import (
    chain_mean_plddt_from_structure_text,
    contains_structure,
    lowest_rank_structure,
    pick_display_structure,
    rank_structure_ranks,
    summary_member_names,
)


NAMES = [
    'results_summary.json',
    'design_results.json',
    'structures/rank_01.pdb',
    'structures/rank_02.pdb',
    'structures/rank_12.cif',
    'structures/product_Ltarget_Dpeptide.pdb',
]


class RankStructureTests(unittest.TestCase):
    def test_rank_map_uses_base_names_and_ignores_non_rank_members(self):
        ranks = rank_structure_ranks(NAMES)
        self.assertEqual(ranks, {'rank_01.pdb': 1, 'rank_02.pdb': 2, 'rank_12.cif': 12})

    def test_contains_matches_base_name_or_directory_suffix(self):
        self.assertTrue(contains_structure(NAMES, 'rank_02.pdb'))
        self.assertTrue(contains_structure(NAMES, 'RANK_02.PDB'))
        self.assertTrue(contains_structure(['structures/rank_07.pdb'], 'rank_07.pdb'))
        self.assertFalse(contains_structure(NAMES, 'rank_03.pdb'))
        self.assertFalse(contains_structure(NAMES, ''))

    def test_lowest_rank_picks_minimum_and_tolerates_absence(self):
        self.assertEqual(lowest_rank_structure(NAMES), 'rank_01.pdb')
        self.assertEqual(lowest_rank_structure(['structures/rank_09.pdb']), 'rank_09.pdb')
        self.assertEqual(lowest_rank_structure(['results_summary.json']), '')

    def test_summary_member_lookup(self):
        self.assertEqual(
            summary_member_names(NAMES),
            ('results_summary.json', 'design_results.json'),
        )
        self.assertEqual(summary_member_names(['other.json']), (None, None))


class PickDisplayStructureTests(unittest.TestCase):
    ROWS_ALL_LINKED = [
        {'rank': 1, 'sequence': 'AAA', 'structure_name': 'rank_01.pdb'},
        {'rank': 2, 'sequence': 'BBB', 'structure_name': 'rank_02.pdb'},
    ]
    ROWS_RANK1_MISSING = [
        {'rank': 1, 'sequence': 'AAA', 'structure_name': ''},
        {'rank': 2, 'sequence': 'BBB', 'structure_name': 'rank_02.pdb'},
        {'rank': 3, 'sequence': 'CCC', 'structure_name': ''},
    ]
    ROWS_NO_FIELDS = [
        {'rank': 1, 'sequence': 'AAA'},
        {'rank': 2, 'sequence': 'BBB', 'structure_name': 'rank_99.pdb'},
    ]

    def test_rank1_structure_wins_when_present(self):
        self.assertEqual(pick_display_structure(self.ROWS_ALL_LINKED, NAMES), 'rank_01.pdb')

    def test_falls_forward_to_first_row_with_existing_structure(self):
        self.assertEqual(pick_display_structure(self.ROWS_RANK1_MISSING, NAMES), 'rank_02.pdb')

    def test_stale_structure_name_falls_to_next_existing(self):
        rows = [
            {'rank': 1, 'sequence': 'AAA', 'structure_name': 'rank_77.pdb'},
            {'rank': 2, 'sequence': 'BBB', 'structure_name': 'rank_12.cif'},
        ]
        self.assertEqual(pick_display_structure(rows, NAMES), 'rank_12.cif')

    def test_legacy_archive_without_row_fields_uses_lowest_rank(self):
        self.assertEqual(pick_display_structure(self.ROWS_NO_FIELDS, NAMES), 'rank_01.pdb')

    def test_empty_everything_is_empty(self):
        self.assertEqual(pick_display_structure([], ['results_summary.json']), '')


class ChainMeanPlddtTests(unittest.TestCase):
    def test_chain_means_ignore_header_and_low_counts(self):
        text = '\n'.join([
            'HEADER  x',
            'ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00 80.00           C',
            'ATOM      2  CA  ALA A   2       1.000   0.000   0.000  1.00 90.00           C',
            'ATOM      3  CA  GLY A   3       2.000   0.000   0.000  1.00 85.00           C',
            'ATOM      4  CA  ALA A   4       3.000   0.000   0.000  1.00  0.00           C',
            'ATOM      5  CA  ALA B   1       4.000   0.000   0.000  1.00 70.00           C',
            'END',
        ])
        means = chain_mean_plddt_from_structure_text(text)
        self.assertEqual(means, {'A': 85.0})

    def test_malformed_b_factors_are_skipped(self):
        text = '\n'.join([
            'ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00 xxxx           C',
            'ATOM      2  CA  ALA A   2       1.000   0.000   0.000  1.00 90.00           C',
        ])
        self.assertEqual(chain_mean_plddt_from_structure_text(text), {})


if __name__ == '__main__':
    unittest.main()
