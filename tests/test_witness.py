"""Ambiguity-witness tests: node-aligned first-divergence view."""

import json
import unittest

from app.engine import analyze
from app.grammar import build_grammar
from app.witness import (
    PRODUCTION_CHOICE,
    SPAN_SPLIT,
    WitnessError,
    build_witness,
    witness_for_conclusion,
)


def G(nts, prods, start, tokens=None):
    return build_grammar({"nonterminals": nts, "productions": prods,
                          "start": start, "tokens": tokens or []})


def P(pid, lhs, rhs):
    return {"id": pid, "lhs": lhs, "rhs": rhs}


def ambiguous_witness(nts, prods, start, tokens):
    conclusion = analyze(G(nts, prods, start, tokens))
    assert conclusion["verdict"] == "AMBIGUOUS_ACCEPTED"
    witness = witness_for_conclusion(conclusion)
    assert witness is not None
    return conclusion, witness


class TestDivergenceLocation(unittest.TestCase):
    def test_divergence_at_root(self):
        # S -> a (pid 1) | a (pid 2): the roots themselves disagree.
        _, w = ambiguous_witness(["S"], [P(1, "S", ["a"]), P(2, "S", ["a"])],
                                 "S", ["a"])
        self.assertEqual(w["shared_prefix"], [])
        div = w["divergence"]
        self.assertEqual(div["symbol"], "S")
        self.assertEqual(div["kind"], PRODUCTION_CHOICE)
        self.assertEqual(div["span"], [0, 1])
        self.assertEqual(div["first"]["production"], 1)
        self.assertEqual(div["second"]["production"], 2)
        self.assertEqual(div["first"]["subtree_summary"]["yield"], ["a"])
        self.assertEqual(div["second"]["subtree_summary"]["yield"], ["a"])

    def test_nested_divergence_prefix_path(self):
        # Root production shared; the choice differs one level down.
        prods = [P(1, "S", ["x", "A", "y"]), P(2, "A", ["a"]), P(3, "A", ["a"])]
        _, w = ambiguous_witness(["S", "A"], prods, "S", ["x", "a", "y"])
        self.assertEqual(
            w["shared_prefix"],
            [{"depth": 0, "symbol": "S", "production": 1, "span": [0, 3],
              "diverging_child_index": 1}],
        )
        div = w["divergence"]
        self.assertEqual(div["symbol"], "A")
        self.assertEqual(div["kind"], PRODUCTION_CHOICE)
        self.assertEqual(div["span"], [1, 2])
        self.assertEqual(div["first"]["production"], 2)
        self.assertEqual(div["second"]["production"], 3)

    def test_expression_ambiguity_diverges_at_root(self):
        prods = [P(1, "E", ["E", "+", "E"]),
                 P(2, "E", ["E", "*", "E"]),
                 P(3, "E", ["id"])]
        conclusion, w = ambiguous_witness(
            ["E"], prods, "E", ["id", "+", "id", "*", "id"])
        self.assertEqual(conclusion["production_sequences"]["first"],
                         [1, 3, 2, 3, 3])
        self.assertEqual(w["shared_prefix"], [])
        div = w["divergence"]
        self.assertEqual((div["symbol"], div["kind"], div["span"]),
                         ("E", PRODUCTION_CHOICE, [0, 5]))
        self.assertEqual(div["first"]["production"], 1)
        self.assertEqual(div["second"]["production"], 2)
        # Each side's follow-up subtree is summarized, not dumped.
        self.assertEqual(div["first"]["subtree_summary"]["production_sequence"],
                         [1, 3, 2, 3, 3])
        self.assertEqual(div["second"]["subtree_summary"]["production_sequence"],
                         [2, 1, 3, 3, 3])
        self.assertEqual(div["first"]["subtree_summary"]["yield"],
                         ["id", "+", "id", "*", "id"])


class TestNodeAlignmentNotPidSequence(unittest.TestCase):
    """Empty RHS, same pid at different spans and left recursion must
    still be located at concrete (symbol, span) nodes."""

    def test_epsilon_and_left_recursion_span_split(self):
        # "a" = (A=>eps, B=>a) or (A=>A a with A=>eps, B=>eps): the two
        # stable sequences [1,2,3,5] and [1,3,4] first differ at preorder
        # index 1, but the reviewable fact is that the A node covers
        # [0,1] on one side and [0,0] on the other.
        prods = [P(1, "S", ["A", "B"]),
                 P(2, "A", ["A", "a"]),   # left recursion (consuming)
                 P(3, "A", []),           # epsilon
                 P(4, "B", ["a"]),
                 P(5, "B", [])]           # epsilon
        conclusion, w = ambiguous_witness(["S", "A", "B"], prods, "S", ["a"])
        self.assertEqual(conclusion["production_sequences"],
                         {"first": [1, 2, 3, 5], "second": [1, 3, 4]})
        self.assertEqual(
            w["shared_prefix"],
            [{"depth": 0, "symbol": "S", "production": 1, "span": [0, 1],
              "diverging_child_index": 0}],
        )
        div = w["divergence"]
        self.assertEqual(div["symbol"], "A")
        self.assertEqual(div["kind"], SPAN_SPLIT)
        self.assertNotIn("span", div)  # spans differ: reported per side
        self.assertEqual(div["first"]["production"], 2)
        self.assertEqual(div["first"]["span"], [0, 1])
        self.assertEqual(div["second"]["production"], 3)
        self.assertEqual(div["second"]["span"], [0, 0])
        s1 = div["first"]["subtree_summary"]
        s2 = div["second"]["subtree_summary"]
        self.assertEqual(s1["production_sequence"], [2, 3])
        self.assertEqual(s1["yield"], ["a"])
        self.assertEqual(s1["node_count"], 3)  # A -> A(eps) a
        self.assertEqual(s2["production_sequence"], [3])
        self.assertEqual(s2["yield"], [])
        self.assertEqual(s2["node_count"], 1)

    def test_same_production_id_at_different_spans(self):
        # S -> S S | a over "aaa": pid 1 occurs at span [0,3] and [1,3]
        # in the first tree but at [0,3] and [0,2] in the second.  A
        # preorder-pid diff cannot express this; the witness must name
        # the actual nodes.
        prods = [P(1, "S", ["S", "S"]), P(2, "S", ["a"])]
        conclusion, w = ambiguous_witness(["S"], prods, "S", ["a", "a", "a"])
        self.assertEqual(conclusion["production_sequences"],
                         {"first": [1, 1, 2, 2, 2],
                          "second": [1, 2, 1, 2, 2]})
        self.assertEqual(
            w["shared_prefix"],
            [{"depth": 0, "symbol": "S", "production": 1, "span": [0, 3],
              "diverging_child_index": 0}],
        )
        div = w["divergence"]
        self.assertEqual(div["symbol"], "S")
        self.assertEqual(div["kind"], SPAN_SPLIT)
        self.assertEqual(div["first"]["production"], 1)
        self.assertEqual(div["first"]["span"], [0, 2])
        self.assertEqual(div["second"]["production"], 2)
        self.assertEqual(div["second"]["span"], [0, 1])
        self.assertEqual(div["first"]["subtree_summary"]["production_sequence"],
                         [1, 2, 2])
        self.assertEqual(div["first"]["subtree_summary"]["yield"], ["a", "a"])

    def test_left_recursion_chain_aligned_by_span(self):
        # Deep left-recursive spines: identical pids recur at shrinking
        # spans; the divergence sits at the innermost epsilon choice.
        prods = [P(1, "S", ["A", "b"]),
                 P(2, "A", ["A", "a"]),
                 P(3, "A", []),
                 P(4, "A", ["a"])]
        # "a b": A=>eps then... no: A must derive "a" via [2,3] or [4].
        conclusion, w = ambiguous_witness(["S", "A"], prods, "S", ["a", "b"])
        self.assertEqual(conclusion["production_sequences"],
                         {"first": [1, 2, 3], "second": [1, 4]})
        self.assertEqual(len(w["shared_prefix"]), 1)
        div = w["divergence"]
        self.assertEqual(div["symbol"], "A")
        self.assertEqual(div["kind"], PRODUCTION_CHOICE)
        self.assertEqual(div["span"], [0, 1])
        self.assertEqual(div["first"]["production"], 2)
        self.assertEqual(div["second"]["production"], 4)


class TestWitnessDerivationRules(unittest.TestCase):
    def test_unique_conclusion_has_no_witness(self):
        g = G(["S"], [P(1, "S", ["a"])], "S", ["a"])
        conclusion = analyze(g)
        self.assertEqual(conclusion["verdict"], "UNIQUE_ACCEPTED")
        self.assertIsNone(witness_for_conclusion(conclusion))

    def test_rejected_conclusion_has_no_witness(self):
        self.assertIsNone(witness_for_conclusion(
            {"verdict": "REJECTED",
             "rejection": {"reason": "INPUT_NOT_ACCEPTED", "detail": "..."}}))

    def test_witness_rederived_from_sealed_trees_only(self):
        # A JSON round-trip simulates a restart: the witness re-derived
        # from the reloaded sealed entry must be identical.
        prods = [P(1, "S", ["A", "B"]), P(2, "A", ["A", "a"]), P(3, "A", []),
                 P(4, "B", ["a"]), P(5, "B", [])]
        conclusion = analyze(G(["S", "A", "B"], prods, "S", ["a"]))
        reloaded = json.loads(json.dumps(conclusion))
        self.assertEqual(witness_for_conclusion(conclusion),
                         witness_for_conclusion(reloaded))

    def test_identical_trees_refuse_fabrication(self):
        g = G(["S"], [P(1, "S", ["a"])], "S", ["a"])
        tree = analyze(g)["tree"]
        with self.assertRaises(WitnessError):
            build_witness(tree, tree)


if __name__ == "__main__":
    unittest.main()
