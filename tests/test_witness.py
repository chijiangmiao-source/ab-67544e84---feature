"""Ambiguity witness tests (run with: python -m unittest discover -s tests).

The witness is re-derived from two already selected/sealed derivation
trees by aligning nonterminal nodes on their symbol and input span --
never by comparing preorder production-id positions.  These tests pin
that behaviour for empty productions (zero-width nodes), left-recursive
expansions and identical pids appearing at different spans, and verify
that identical trees yield no pseudo-evidence.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from app.engine import ambiguity_witness, analyze
from app.grammar import build_grammar
from app.service import make_server
from app.storage import SealedStore


def G(nts, prods, start, tokens):
    return build_grammar({"nonterminals": nts, "productions": prods,
                          "start": start, "tokens": tokens})


def P(pid, lhs, rhs):
    return {"id": pid, "lhs": lhs, "rhs": rhs}


def witness_of(g):
    r = analyze(g)
    assert r["verdict"] == "AMBIGUOUS_ACCEPTED", r["verdict"]
    return r, ambiguity_witness(r["trees"]["first"], r["trees"]["second"])


def all_spans(summary):
    out = [tuple(summary["span"])]
    for child in summary.get("children", []):
        out.extend(all_spans(child))
    return out


class TestWitnessAlignment(unittest.TestCase):
    def test_root_divergence(self):
        g = G(["S"], [P(1, "S", ["a"]), P(2, "S", ["a"])], "S", ["a"])
        _, w = witness_of(g)
        self.assertEqual(w["shared_prefix"], [])
        d = w["divergence"]
        self.assertEqual(d["symbol"], "S")
        self.assertEqual(d["span_first"], [0, 1])
        self.assertEqual(d["span_second"], [0, 1])
        self.assertEqual(d["first"]["production"], 1)
        self.assertEqual(d["second"]["production"], 2)

    def test_epsilon_diverges_at_zero_span_node(self):
        # S -> A x ; A -> B | C ; B -> eps ; C -> eps : the two trees
        # differ only at the zero-width node A[0,0].
        prods = [P(1, "S", ["A", "x"]), P(2, "A", ["B"]), P(3, "A", ["C"]),
                 P(4, "B", []), P(5, "C", [])]
        g = G(["S", "A", "B", "C"], prods, "S", ["x"])
        r, w = witness_of(g)
        self.assertEqual([(p["symbol"], tuple(p["span"])) for p in w["shared_prefix"]],
                         [("S", (0, 1))])
        d = w["divergence"]
        self.assertEqual(d["symbol"], "A")
        self.assertEqual(d["span_first"], [0, 0])
        self.assertEqual(d["span_second"], [0, 0])
        self.assertEqual((d["first"]["production"], d["second"]["production"]), (2, 3))
        # The continuation summaries carry the epsilon leaves.
        self.assertEqual(d["first"]["subtree_summary"]["production_sequence"], [2, 4])
        self.assertEqual(d["second"]["subtree_summary"]["production_sequence"], [3, 5])
        # The witness must match the sealed trees it was derived from.
        self.assertEqual(d["first"]["production"],
                         r["trees"]["first"]["children"][0]["production"])

    def test_left_recursion_descends_to_actual_inner_node(self):
        # S -> S a | a (pid 2) | a (pid 3); "aa": both trees share the
        # outer pid1 node S[0,2]; the first divergence is the inner
        # S[0,1], where pid 1 itself also appears in the first tree --
        # span alignment, not pid-position matching, locates it.
        prods = [P(1, "S", ["S", "a"]), P(2, "S", ["a"]), P(3, "S", ["a"])]
        g = G(["S"], prods, "S", ["a", "a"])
        _, w = witness_of(g)
        self.assertEqual([(p["symbol"], p["production"], tuple(p["span"]))
                          for p in w["shared_prefix"]],
                         [("S", 1, (0, 2))])
        d = w["divergence"]
        self.assertEqual(d["symbol"], "S")
        self.assertEqual(d["span_first"], [0, 1])
        self.assertEqual(d["span_second"], [0, 1])
        self.assertEqual((d["first"]["production"], d["second"]["production"]), (2, 3))

    def test_same_pid_different_span_is_not_aligned(self):
        # "a": S -> A B ; A -> a (3) | eps (4) ; B -> a (2) | eps (5).
        # first: A spans [0,1] (pid3), B is epsilon [1,1];
        # second: A is epsilon [0,0] (pid4), B spans [0,1] (pid2).
        # The shared root is S[0,1] pid1; at the A slot the spans already
        # differ, so the divergence must be reported there with both
        # spans -- alignment cannot rely on pid positions.
        prods = [P(1, "S", ["A", "B"]), P(2, "B", ["a"]), P(3, "A", ["a"]),
                 P(4, "A", []), P(5, "B", [])]
        g = G(["S", "A", "B"], prods, "S", ["a"])
        r, w = witness_of(g)
        self.assertEqual(r["production_sequences"],
                         {"first": [1, 3, 5], "second": [1, 4, 2]})
        d = w["divergence"]
        self.assertEqual(d["symbol"], "A")
        self.assertEqual(d["span_first"], [0, 1])
        self.assertEqual(d["span_second"], [0, 0])
        self.assertEqual((d["first"]["production"], d["second"]["production"]), (3, 4))

    def test_epsilon_inside_left_recursive_expansion(self):
        # Shared outer S -> S a [0,2]; inner S[0,1] is either a further
        # S->S a whose left child is S->A->eps (zero-width [0,0]), or
        # S->a directly.  Both tricky mechanisms appear in one witness.
        prods = [P(1, "S", ["S", "a"]), P(2, "S", ["a"]), P(3, "S", ["A"]),
                 P(4, "A", ["a"]), P(5, "A", [])]
        g = G(["S", "A"], prods, "S", ["a", "a"])
        _, w = witness_of(g)
        self.assertEqual([(p["production"], tuple(p["span"]))
                          for p in w["shared_prefix"]], [(1, (0, 2))])
        d = w["divergence"]
        self.assertEqual((d["symbol"], d["span_first"], d["span_second"]),
                         ("S", [0, 1], [0, 1]))
        self.assertEqual((d["first"]["production"], d["second"]["production"]), (1, 2))
        # The first-side summary reaches the epsilon production at [0,0].
        spans_first = all_spans(d["first"]["subtree_summary"])
        self.assertIn((0, 0), spans_first)
        self.assertEqual(
            d["first"]["subtree_summary"]["production_sequence"], [1, 3, 5])

    def test_nested_divergence_skips_only_shared_prefix(self):
        # S -> x A y ; A -> a (2) | a (3): root pid1 is shared, the
        # divergence is the nested A[1,2], not the root.
        prods = [P(1, "S", ["x", "A", "y"]), P(2, "A", ["a"]), P(3, "A", ["a"])]
        g = G(["S", "A"], prods, "S", ["x", "a", "y"])
        _, w = witness_of(g)
        self.assertEqual([(p["symbol"], tuple(p["span"])) for p in w["shared_prefix"]],
                         [("S", (0, 3))])
        d = w["divergence"]
        self.assertEqual((d["symbol"], d["span_first"]), ("A", [1, 2]))
        self.assertEqual((d["first"]["production"], d["second"]["production"]), (2, 3))

    def test_identical_trees_raise_no_pseudo_evidence(self):
        g = G(["S"], [P(1, "S", ["a"])], "S", ["a"])
        tree = analyze(g)["tree"]
        with self.assertRaises(ValueError):
            ambiguity_witness(tree, tree)


# ---------------------------------------------------------------------------
# HTTP view
# ---------------------------------------------------------------------------


class WitnessServiceHarness:
    def __init__(self, path):
        self.httpd, _ = make_server("127.0.0.1", 0, SealedStore(path))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def post(self, payload):
        req = urllib.request.Request(
            self.base + "/api/v1/analyze", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


class TestWitnessEndpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "sealed.json")
        self.h = WitnessServiceHarness(self.path)

    def tearDown(self):
        self.h.stop()
        self.tmp.cleanup()

    AMB = {
        "audit_id": "w-amb", "nonterminals": ["S", "A", "B", "C"],
        "productions": [{"id": 1, "lhs": "S", "rhs": ["A", "x"]},
                        {"id": 2, "lhs": "A", "rhs": ["B"]},
                        {"id": 3, "lhs": "A", "rhs": ["C"]},
                        {"id": 4, "lhs": "B", "rhs": []},
                        {"id": 5, "lhs": "C", "rhs": []}],
        "start": "S", "tokens": ["x"],
    }

    def test_ambiguous_witness_view(self):
        s, _ = self.h.post(self.AMB)
        self.assertEqual(s, 200)
        s, w = self.h.get("/api/v1/witness/w-amb")
        self.assertEqual(s, 200)
        self.assertTrue(w["witness_available"])
        self.assertEqual(w["verdict"], "AMBIGUOUS_ACCEPTED")
        d = w["witness"]["divergence"]
        self.assertEqual(d["symbol"], "A")
        self.assertEqual(d["span_first"], [0, 0])
        self.assertEqual((d["first"]["production"], d["second"]["production"]), (2, 3))
        self.assertIn("production_sequences", w)
        # Idempotent: re-derived from the same sealed trees.
        s, w2 = self.h.get("/api/v1/witness/w-amb")
        self.assertEqual(w, w2)

    def test_stable_across_restart(self):
        self.h.post(self.AMB)
        s, w = self.h.get("/api/v1/witness/w-amb")
        self.assertEqual(s, 200)
        self.h.stop()
        h2 = WitnessServiceHarness(self.path)
        try:
            s, w2 = h2.get("/api/v1/witness/w-amb")
            self.assertEqual(s, 200)
            self.assertEqual(w2, w)
        finally:
            h2.stop()

    def test_unique_conclusion_has_no_witness(self):
        payload = {"audit_id": "w-uniq", "nonterminals": ["S"],
                   "productions": [{"id": 1, "lhs": "S", "rhs": ["a"]}],
                   "start": "S", "tokens": ["a"]}
        self.h.post(payload)
        s, w = self.h.get("/api/v1/witness/w-uniq")
        self.assertEqual(s, 200)
        self.assertFalse(w["witness_available"])
        self.assertNotIn("witness", w)
        self.assertIn("唯一", w["detail"])

    def test_rejected_conclusion_has_no_witness(self):
        payload = {"audit_id": "w-rej", "nonterminals": ["S"],
                   "productions": [{"id": 1, "lhs": "S", "rhs": ["a"]}],
                   "start": "S", "tokens": ["z"]}
        self.h.post(payload)
        s, w = self.h.get("/api/v1/witness/w-rej")
        self.assertEqual(s, 200)
        self.assertFalse(w["witness_available"])
        self.assertNotIn("witness", w)

    def test_unknown_id_keeps_read_semantics(self):
        s, w = self.h.get("/api/v1/witness/does-not-exist")
        self.assertEqual(s, 404)
        self.assertEqual(w["error"], "NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
