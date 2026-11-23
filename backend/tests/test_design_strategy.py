from __future__ import annotations

import pytest

from backend.runtime.design_strategy import Candidate, DesignStrategy, Esm3Strategy, keep_best_by_prior


class FakeProposer:
    def __init__(self, proposals, perplexity=None):
        self.proposals = proposals
        self._perplexity = perplexity or {}
        self.calls = []

    def propose(self, natural_pool, unnatural_pool, elites, count):
        self.calls.append(("propose", natural_pool, unnatural_pool, elites, count))
        return self.proposals

    def pseudo_perplexity(self, sequence):
        return self._perplexity.get(sequence)

    def learn(self, elites, rows):
        self.calls.append(("learn", elites, rows))


def test_esm3_strategy_passes_pools_elites_and_budget_to_the_proposer():
    proposer = FakeProposer([
        ("ACDE", [{"ccd": "NLE", "position": 2}], [1, 4], "denovo"),
        ("FGHI", None, None, "edit"),
    ])
    strategy = Esm3Strategy(proposer, ["A", "C"], [{"ccd": "NLE"}])
    elites = [{"sequence": "AAAA"}]
    assert list(strategy.propose(elites, 12)) == [
        Candidate("ACDE", [{"ccd": "NLE", "position": 2}], [1, 4], "denovo"),
        Candidate("FGHI", [], [], "edit"),
    ]
    assert proposer.calls == [("propose", ["A", "C"], [{"ccd": "NLE"}], elites, 12)]


def test_esm3_strategy_delegates_prior_score_and_learning():
    proposer = FakeProposer([], {"ACDE": 3.5})
    strategy = Esm3Strategy(proposer, [], [])
    assert strategy.prior_score("ACDE") == 3.5
    assert strategy.prior_score("XXXX") is None
    strategy.learn([{"sequence": "ACDE"}], [{"sequence": "ACDE", "composite_score": 0.7}])
    assert proposer.calls == [
        ("learn", [{"sequence": "ACDE"}], [{"sequence": "ACDE", "composite_score": 0.7}])]


def test_keep_best_by_prior_orders_by_score_and_ranks_unscored_last():
    rows = [{"sequence": "AAAA"}, {"sequence": "BBBB"}, {"sequence": "CCCC"}, {"sequence": "DDDD"}]
    scores = {"AAAA": 2.0, "BBBB": 1.0, "CCCC": 9.0}
    assert keep_best_by_prior(scores.get, rows, 2) == [{"sequence": "BBBB"}, {"sequence": "AAAA"}]
    assert keep_best_by_prior(lambda _sequence: None, rows, 2) == [
        {"sequence": "AAAA"}, {"sequence": "BBBB"}]


def test_subclasses_must_implement_every_action():
    class Incomplete(DesignStrategy):
        name = "incomplete"

    with pytest.raises(TypeError):
        Incomplete()
    assert Esm3Strategy.name == "esm3"
    assert isinstance(Esm3Strategy(FakeProposer([]), [], []), DesignStrategy)
