"""Ambiguity witness: first-divergence view over two sealed trees.

When a reviewer reopens a sealed ``AMBIGUOUS_ACCEPTED`` conclusion they
need to know *where* the two stable derivation trees first make
different choices, without diffing the complete trees by hand.  The
witness is re-derived **only** from the two finite trees stored in the
sealed conclusion, so the view is identical for every replay and across
restarts.

Alignment advances over derivation nodes keyed by ``(symbol, input
span)`` -- never over preorder production-id sequences -- so empty
right-hand sides, the same production id applied at different spans and
left-recursive expansions are all pinned to concrete, reviewable nodes:

* both trees are walked from the root in parallel;
* while the aligned nodes agree on symbol, span and production, the walk
  descends into the first child pair that differs (the children of one
  production cover the parent's span left to right, so child order is
  input order);
* the first aligned pair that disagrees is the divergence.  Both nodes
  occupy the same right-hand-side slot of the same shared parent, hence
  the same nonterminal; each side is reported with its production id,
  its token span and a summary of the subtree that follows.

The longest shared structural prefix is the spine of shared ancestors
from the root down to (and including) the parent of the diverging pair.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .engine import AMBIGUOUS

# Divergence kinds.
PRODUCTION_CHOICE = "PRODUCTION_CHOICE"  # same symbol+span node, different production
SPAN_SPLIT = "SPAN_SPLIT"                # same symbol, different span coverage

ALIGNMENT_RULE = (
    "按派生节点的（符号, 输入跨度）对齐并行推进，首个不一致的节点对即首次分歧；"
    "不比较先序产生式编号序列，因此空右部、同编号不同跨度与左递归展开均定位到实际节点"
)


class WitnessError(Exception):
    """The two sealed trees do not allow a divergence to be located."""


# ---------------------------------------------------------------------------
# Subtree summaries (derived purely from the sealed tree nodes)
# ---------------------------------------------------------------------------


def _preorder_pids(node: dict) -> List[int]:
    if "token" in node:
        return []
    out = [node["production"]]
    for child in node["children"]:
        out.extend(_preorder_pids(child))
    return out


def _yield(node: dict) -> List[str]:
    if "token" in node:
        return [node["token"]]
    out: List[str] = []
    for child in node["children"]:
        out.extend(_yield(child))
    return out


def _node_count(node: dict) -> int:
    if "token" in node:
        return 1
    return 1 + sum(_node_count(c) for c in node["children"])


def _max_depth(node: dict) -> int:
    if "token" in node or not node.get("children"):
        return 0
    return 1 + max(_max_depth(c) for c in node["children"])


def _subtree_summary(node: dict) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "span": list(node["span"]),
        "yield": _yield(node),
        "node_count": _node_count(node),
        "max_depth": _max_depth(node),
    }
    if "token" in node:
        summary["token"] = node["token"]
    else:
        summary["symbol"] = node["symbol"]
        summary["production"] = node["production"]
        summary["production_sequence"] = _preorder_pids(node)
    return summary


def _side(node: dict) -> Dict[str, Any]:
    """One side of the divergence: the choice made and what follows."""
    side: Dict[str, Any] = {"span": list(node["span"])}
    if "token" in node:  # defensive: aligned divergence is always a NT node
        side["token"] = node["token"]
    else:
        side["production"] = node["production"]
    side["subtree_summary"] = _subtree_summary(node)
    return side


# ---------------------------------------------------------------------------
# Node-aligned first-divergence walk
# ---------------------------------------------------------------------------


def build_witness(first: dict, second: dict) -> Dict[str, Any]:
    """Locate the first divergence between two finite derivation trees.

    ``first``/``second`` are the rendered trees stored in a sealed
    ``AMBIGUOUS_ACCEPTED`` conclusion.  Returns the witness view:
    ``shared_prefix`` (the longest shared structural prefix, root first),
    the ``divergence`` node pair and a human-readable ``detail``.
    """
    prefix: List[Dict[str, Any]] = []
    u, v = first, second
    depth = 0
    while (
        "token" not in u
        and "token" not in v
        and u["symbol"] == v["symbol"]
        and u["span"] == v["span"]
        and u["production"] == v["production"]
    ):
        children_u = u["children"]
        children_v = v["children"]
        diverging = None
        # Same production -> same arity; child order is input order.
        for idx, (cu, cv) in enumerate(zip(children_u, children_v)):
            if cu != cv:
                diverging = idx
                break
        if diverging is None:
            # Two distinct sealed trees can never reach this; guard
            # against corrupt evidence rather than fabricate a witness.
            raise WitnessError(
                "两棵派生树在共享节点下的子树完全一致，无法定位首次分歧"
            )
        prefix.append(
            {
                "depth": depth,
                "symbol": u["symbol"],
                "production": u["production"],
                "span": list(u["span"]),
                "diverging_child_index": diverging,
            }
        )
        u = children_u[diverging]
        v = children_v[diverging]
        depth += 1

    symbol = u.get("symbol", v.get("symbol"))
    same_span = list(u["span"]) == list(v["span"])
    kind = PRODUCTION_CHOICE if same_span else SPAN_SPLIT
    divergence: Dict[str, Any] = {
        "symbol": symbol,
        "kind": kind,
        "first": _side(u),
        "second": _side(v),
    }
    if same_span:
        divergence["span"] = list(u["span"])

    fp = divergence["first"].get("production")
    sp = divergence["second"].get("production")
    if same_span:
        where = f"非终结符 {symbol}（词元跨度 {list(u['span'])}）"
    else:
        where = (
            f"非终结符 {symbol}（两侧词元跨度不同：first {list(u['span'])}，"
            f"second {list(v['span'])}）"
        )
    detail = (
        f"两树共享 {len(prefix)} 层结构前缀后，首次分歧于{where}："
        f"first 采用产生式 {fp}，second 采用产生式 {sp}"
    )

    return {
        "alignment_rule": ALIGNMENT_RULE,
        "shared_prefix": prefix,
        "divergence": divergence,
        "detail": detail,
    }


def witness_for_conclusion(conclusion: dict) -> Optional[Dict[str, Any]]:
    """Re-derive the ambiguity witness from a sealed conclusion.

    Returns ``None`` when the conclusion is not an ambiguous acceptance:
    a unique acceptance has a single tree and a rejection has none, so
    no ambiguity witness exists for either.
    """
    if conclusion.get("verdict") != AMBIGUOUS:
        return None
    trees = conclusion["trees"]
    return build_witness(trees["first"], trees["second"])
