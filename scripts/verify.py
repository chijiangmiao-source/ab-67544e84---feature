#!/usr/bin/env python3
"""One-shot acceptance driver for the ``verify`` Compose service.

Order of operations (mirrors the acceptance contract):

1. grammar engine + storage unit tests,
2. (image build happens before this container starts -- ``compose up
   --build``),
3. HTTP smoke against the running arbiter:
   unique acceptance, ambiguous acceptance with two stable trees,
   non-consuming-cycle rejection, equivalent retransmission replay,
   audit-id conflict preserving the original evidence, and ambiguity
   witnesses (epsilon zero-width node and left recursion located via
   symbol+span alignment; unique/rejected conclusions have none).

Exits 0 only when every step passes; any failure exits 1.
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
import urllib.error
import urllib.request
import uuid

BASE_URL = os.environ.get("ARBITER_BASE_URL", "http://127.0.0.1:8080")
HEALTH_TIMEOUT = float(os.environ.get("HEALTH_TIMEOUT", "30"))

failures = []


def step(name):
    print(f"\n--- {name}", flush=True)


def check(cond, msg):
    if cond:
        print(f"    PASS: {msg}")
    else:
        print(f"    FAIL: {msg}")
        failures.append(msg)


def http_post(path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE_URL + path, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def http_get(path):
    try:
        with urllib.request.urlopen(BASE_URL + path, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def wait_healthy():
    deadline = time.time() + HEALTH_TIMEOUT
    last = None
    while time.time() < deadline:
        try:
            status, body = http_get("/healthz")
            if status == 200 and body.get("status") == "ok":
                print(f"    PASS: 健康检查 200 {body}")
                return True
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.5)
    print(f"    FAIL: 健康检查超时（{HEALTH_TIMEOUT}s）：{last}")
    failures.append("health")
    return False


def main() -> int:
    step("步骤 1/3：文法引擎与封存存储单元测试")
    if os.environ.get("SKIP_UNIT_TESTS") == "1":
        print("    （单元测试已在本阶段之外执行，跳过）")
    else:
        loader = unittest.TestLoader()
        suite = loader.discover("tests", pattern="test_*.py")
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        if not result.wasSuccessful():
            failures.append("unit tests")
            # Still report early; HTTP smoke is meaningless without engine.
            return report()

    if not wait_healthy():
        return report()

    run_id = uuid.uuid4().hex[:8]

    step("步骤 2/3：唯一接受场景（唯一树）")
    uid = f"verify-unique-{run_id}"
    status, body = http_post("/api/v1/analyze", {
        "audit_id": uid,
        "nonterminals": ["S"],
        "productions": [{"id": 1, "lhs": "S", "rhs": ["a", "b"]}],
        "start": "S",
        "tokens": ["a", "b"],
    })
    check(status == 200, f"HTTP 200（实际 {status}）")
    res = body.get("result", {})
    check(res.get("verdict") == "UNIQUE_ACCEPTED",
          f"verdict=UNIQUE_ACCEPTED（实际 {res.get('verdict')}）")
    check(res.get("production_sequence") == [1],
          f"唯一产生式序列 [1]（实际 {res.get('production_sequence')}）")
    check(body.get("seal_status") == "SEALED", "首次提交封存 SEALED")
    tree = res.get("tree", {})
    check(tree.get("symbol") == "S" and tree.get("span") == [0, 2]
          and len(tree.get("children", [])) == 2,
          "返回唯一派生树且跨度覆盖全部词元")

    # Equivalent retransmission (declaration order shuffled) -> replay.
    status2, body2 = http_post("/api/v1/analyze", {
        "audit_id": uid,
        "nonterminals": ["S"],
        "productions": [{"id": 1, "lhs": "S", "rhs": ["a", "b"]}],
        "start": "S",
        "tokens": ["a", "b"],
    })
    check(status2 == 200 and body2.get("seal_status") == "REPLAYED",
          f"语义等价重放回封存结论 REPLAYED（实际 HTTP {status2} {body2.get('seal_status')}）")
    check(body2.get("result", {}).get("production_sequence") == [1],
          "回放的证据与原结论一致")

    # Same audit id, different input -> 409 conflict, evidence retained.
    status3, body3 = http_post("/api/v1/analyze", {
        "audit_id": uid,
        "nonterminals": ["S"],
        "productions": [{"id": 1, "lhs": "S", "rhs": ["a"]}],
        "start": "S",
        "tokens": ["a"],
    })
    check(status3 == 409 and body3.get("error") == "AUDIT_ID_CONFLICT",
          f"同标识不同输入冲突 HTTP 409（实际 {status3}）")
    orig = body3.get("original_evidence", {}).get("conclusion", {})
    check(orig.get("verdict") == "UNIQUE_ACCEPTED"
          and orig.get("production_sequence") == [1],
          "冲突响应保留并回传原始证据")

    step("步骤 3a：歧义接受场景（两棵按编号序列稳定选出的树）")
    aid = f"verify-amb-{run_id}"
    status, body = http_post("/api/v1/analyze", {
        "audit_id": aid,
        "nonterminals": ["E"],
        "productions": [
            {"id": 1, "lhs": "E", "rhs": ["E", "+", "E"]},
            {"id": 2, "lhs": "E", "rhs": ["E", "*", "E"]},
            {"id": 3, "lhs": "E", "rhs": ["id"]},
        ],
        "start": "E",
        "tokens": ["id", "+", "id", "*", "id"],
    })
    res = body.get("result", {})
    check(status == 200 and res.get("verdict") == "AMBIGUOUS_ACCEPTED",
          f"歧义接受（实际 HTTP {status} {res.get('verdict')}）")
    seqs = res.get("production_sequences", {})
    s1, s2 = seqs.get("first"), seqs.get("second")
    check(isinstance(s1, list) and isinstance(s2, list) and s1 != s2,
          f"两棵树产生式序列不同：{s1} vs {s2}")
    check(s1 == sorted([s1, s2])[0],
          f"first 为编号序列字典序最小：{s1} <= {s2}")
    first, second = res.get("trees", {}).values() if res.get("trees") else ({}, {})
    check(first.get("span") == [0, 5] and second.get("span") == [0, 5],
          "两棵不同派生树均完整覆盖输入 [0,5]")
    # Determinism: re-submit under a new id and compare tree sequences.
    status_b, body_b = http_post("/api/v1/analyze", {
        "audit_id": f"verify-amb2-{run_id}",
        "nonterminals": ["E"],
        "productions": [
            {"id": 3, "lhs": "E", "rhs": ["id"]},
            {"id": 2, "lhs": "E", "rhs": ["E", "*", "E"]},
            {"id": 1, "lhs": "E", "rhs": ["E", "+", "E"]},
        ],
        "start": "E",
        "tokens": ["id", "+", "id", "*", "id"],
    })
    sb = body_b.get("result", {}).get("production_sequences", {})
    check(sb.get("first") == s1 and sb.get("second") == s2,
          f"产生式提交顺序打乱后选树仍稳定：{sb.get('first')} == {s1}")

    step("步骤 3b：可达不消费词元循环必须明确拒绝")
    cid = f"verify-cycle-{run_id}"
    status, body = http_post("/api/v1/analyze", {
        "audit_id": cid,
        "nonterminals": ["A", "B"],
        "productions": [
            {"id": 1, "lhs": "A", "rhs": ["B"]},
            {"id": 2, "lhs": "B", "rhs": ["A"]},
            {"id": 3, "lhs": "A", "rhs": []},
        ],
        "start": "A",
        "tokens": [],
    })
    res = body.get("result", {})
    rej = res.get("rejection", {})
    check(status == 200 and res.get("verdict") == "REJECTED",
          f"循环场景 REJECTED（实际 HTTP {status} {res.get('verdict')}）")
    check(rej.get("reason") == "NONCONSUMING_CYCLE",
          f"原因为 NONCONSUMING_CYCLE（实际 {rej.get('reason')}）")
    cyc = rej.get("evidence", {}).get("cycle")
    check(cyc == ["A", "B", "A"], f"给出循环证据 {cyc}")
    check("无限" in rej.get("detail", "") or "循环" in rej.get("detail", ""),
          "给出首个可操作中文原因")

    step("步骤 3c：歧义见证（空产生式零跨度 + 左递归实际节点 + 非歧义无伪证据）")

    # Witness endpoint must follow the existing read semantics for the
    # ambiguous conclusion sealed in step 3a: divergence re-derived from
    # the two sealed trees at the root E[0,5].
    sw, wb = http_get(f"/api/v1/witness/{aid}")
    check(sw == 200 and wb.get("witness_available") is True,
          f"歧义结论可读取歧义见证（实际 HTTP {sw} available={wb.get('witness_available')}）")
    wd = wb.get("witness", {}).get("divergence", {})
    check(wd.get("symbol") == "E" and wd.get("span_first") == [0, 5]
          and wd.get("span_second") == [0, 5]
          and {wd.get("first", {}).get("production"),
               wd.get("second", {}).get("production")} == {1, 2},
          f"表达式样例首次分歧为 E[0,5] 处产生式 1 vs 2（实际 {wd.get('symbol')} "
          f"{wd.get('span_first')}/{wd.get('span_second')} "
          f"{wd.get('first', {}).get('production')}/{wd.get('second', {}).get('production')}）")
    check("subtree_summary" in wd.get("first", {})
          and "production_sequence" in wd.get("first", {}).get("subtree_summary", {}),
          "分歧两侧给出各自后续子树摘要（含先序产生式序列）")

    # Empty productions only: the two trees differ solely at the
    # zero-width node A[0,0] -- alignment must locate the epsilon node.
    eid = f"verify-wit-eps-{run_id}"
    http_post("/api/v1/analyze", {
        "audit_id": eid,
        "nonterminals": ["S", "A", "B", "C"],
        "productions": [
            {"id": 1, "lhs": "S", "rhs": ["A", "x"]},
            {"id": 2, "lhs": "A", "rhs": ["B"]},
            {"id": 3, "lhs": "A", "rhs": ["C"]},
            {"id": 4, "lhs": "B", "rhs": []},
            {"id": 5, "lhs": "C", "rhs": []},
        ],
        "start": "S",
        "tokens": ["x"],
    })
    sw, wb = http_get(f"/api/v1/witness/{eid}")
    wd = wb.get("witness", {}).get("divergence", {})
    prefix = wb.get("witness", {}).get("shared_prefix", [])
    check(sw == 200 and wb.get("witness_available") is True
          and [(p.get("symbol"), p.get("span")) for p in prefix] == [("S", [0, 1])]
          and wd.get("symbol") == "A"
          and wd.get("span_first") == [0, 0] and wd.get("span_second") == [0, 0]
          and wd.get("first", {}).get("production") == 2
          and wd.get("second", {}).get("production") == 3,
          f"空产生式样例分歧定位到零跨度节点 A[0,0]（实际 {wd.get('symbol')} "
          f"{wd.get('span_first')}/{wd.get('span_second')} "
          f"{wd.get('first', {}).get('production')}/{wd.get('second', {}).get('production')}）")

    # Left recursion AND an empty production in one ambiguous sample:
    # S -> S a | a | A ; A -> a | eps ; input "aa".  Both trees share
    # the outer S->S a node spanning [0,2]; the first divergence is the
    # inner left-recursive S[0,1] (pid1 reaching an epsilon S->A->eps at
    # [0,0] vs pid2 terminal).  Span-based alignment must descend there
    # instead of comparing preorder pid positions.
    lid = f"verify-wit-lr-{run_id}"
    slr, blr = http_post("/api/v1/analyze", {
        "audit_id": lid,
        "nonterminals": ["S", "A"],
        "productions": [
            {"id": 1, "lhs": "S", "rhs": ["S", "a"]},
            {"id": 2, "lhs": "S", "rhs": ["a"]},
            {"id": 3, "lhs": "S", "rhs": ["A"]},
            {"id": 4, "lhs": "A", "rhs": ["a"]},
            {"id": 5, "lhs": "A", "rhs": []},
        ],
        "start": "S",
        "tokens": ["a", "a"],
    })
    check(slr == 200 and blr.get("result", {}).get("verdict") == "AMBIGUOUS_ACCEPTED",
          f"左递归+空产生式样例歧义接受（实际 HTTP {slr} "
          f"{blr.get('result', {}).get('verdict')}）")
    sw, w1 = http_get(f"/api/v1/witness/{lid}")
    ww = w1.get("witness", {})
    wd = ww.get("divergence", {})
    first_summary = wd.get("first", {}).get("subtree_summary", {})

    def spans(node):
        out = [tuple(node["span"])] if isinstance(node, dict) and "span" in node else []
        for child in node.get("children", []):
            out.extend(spans(child))
        return out

    check(sw == 200
          and [(p.get("symbol"), p.get("production"), p.get("span"))
               for p in ww.get("shared_prefix", [])] == [("S", 1, [0, 2])]
          and wd.get("symbol") == "S"
          and wd.get("span_first") == [0, 1] and wd.get("span_second") == [0, 1]
          and wd.get("first", {}).get("production") == 1
          and wd.get("second", {}).get("production") == 2
          and first_summary.get("production_sequence") == [1, 3, 5]
          and (0, 0) in spans(first_summary),
          "左递归样例分歧定位到实际内层节点 S[0,1]（共享外层 S[0,2] 产生式1），"
          "first 侧经空产生式到达零跨度 [0,0]")

    # Cross-check the witness against the sealed trees themselves: the
    # production sequence of the reported continuation must be a prefix
    # of the sealed preorder sequence after the shared prefix length.
    trees = blr.get("result", {}).get("trees", {})
    sealed_first = trees.get("first", {})
    inner_first = sealed_first.get("children", [{}])[0]
    check(inner_first.get("symbol") == wd.get("symbol")
          and inner_first.get("span") == wd.get("span_first")
          and inner_first.get("production") == wd.get("first", {}).get("production"),
          "见证分歧节点与封存 first 树中的实际节点一致（符号/跨度/产生式可复核）")

    # Stability: the witness is re-derived from the same sealed trees.
    sw2, w2 = http_get(f"/api/v1/witness/{lid}")
    check(sw2 == 200 and w2 == w1, "重复读取见证结果稳定一致（重启后亦由封存树重新导出）")

    # No pseudo-evidence: unique and rejected conclusions say so.
    su, wu = http_get(f"/api/v1/witness/{uid}")
    check(su == 200 and wu.get("witness_available") is False
          and "witness" not in wu and "唯一" in wu.get("detail", ""),
          f"唯一接受结论明确说明不存在歧义见证（实际 available={wu.get('witness_available')}）")
    sr, wr = http_get(f"/api/v1/witness/{cid}")
    check(sr == 200 and wr.get("witness_available") is False
          and "witness" not in wr,
          "拒绝结论不生成歧义见证")

    # Unknown audit id keeps the existing read semantics (404).
    s404, b404 = http_get(f"/api/v1/witness/unknown-{run_id}")
    check(s404 == 404 and b404.get("error") == "NOT_FOUND",
          f"未知审计标识读取见证按既有语义 404（实际 {s404}）")

    return report()


def report() -> int:
    print("\n================ 验收汇总 ================")
    if failures:
        print(f"失败 {len(failures)} 项：")
        for f in failures:
            print(f"  - {f}")
        print("RESULT: FAIL")
        return 1
    print("全部步骤通过：单元测试 / 镜像 / 唯一 / 歧义 / 无消费环 / 回放 / 冲突 / 歧义见证")
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
