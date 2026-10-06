# 深空着陆器文法仲裁服务（Shared Packed Parse Forest Arbiter）

地面规则离线转交的故障处置指令，需要确认一串已捕获词元**不会被两条不同的有限推导解释成不同动作序列**。
本服务对任意上下文无关文法（允许空产生式、直接/间接左递归）在**不枚举全部推导**的前提下，
基于共享打包解析森林（Earley + 二值化 SPPF）给出三态判定：

| verdict | 含义 |
|---|---|
| `REJECTED` | 拒绝：词元不被接受 / 起始符号无法派生 / 存在可达的不消费词元循环 |
| `UNIQUE_ACCEPTED` | 唯一接受，返回唯一派生树 |
| `AMBIGUOUS_ACCEPTED` | 歧义接受，返回按产生式编号序列稳定选出的**两棵不同有限派生树** |

## 协议限制

- 非终结符 ≤ 16；产生式 ≤ 32 且编号唯一（正整数）；输入词元 ≤ 48，均为可打印 ASCII
- 产生式右部可为空数组（ε）；文法可含直接或间接左递归
- 可选字段 `terminals` 给出显式词元字母表；提供后，右部未解析符号按**悬空引用**拒绝

## 判定与证据规则

- **共享森林**：每个打包节点只保留字典序最小的**两个**不同先序产生式编号序列；
  树数以饱和值 `{0,1,2}` 计数，因此 48 个高度歧义词元（解析数可达 Catalan 量级）也在毫秒级完成，绝不枚举推导。
- **稳定选树**：先序产生式编号序列与具体派生树一一对应，构成稳定全序。
  `first` = 字典序最小序列，`second` = 字典序次小序列（即在尽量晚的位置分歧，而非首个可分歧点）。
- **可达不消费词元循环**：静态分析构造有限 ε 可消集 R 与零消费边
  （产生式无终结符，且除目标符号外其余符号都在 R 中），从起始符号可达的有向环一律明确拒绝，
  返回环路径与各步产生式编号——无限展开不会被伪装成证据。
- **首个可操作原因**：非法符号、悬空引用、编号重复、超限等结构问题按检查顺序返回首个原因（中文 `detail`）。

## 审计封存

- 新审计标识：计算并封存结论（`SEALED`），落盘到 `/data/sealed.json`（原子替换）。
- 同标识 + 语义等价重传（与声明/产生式数组顺序无关，仅与内容有关）：回放原结论（`REPLAYED`），不重新计算。
- 同标识 + 不同输入：`HTTP 409 AUDIT_ID_CONFLICT`，**保留并回传原证据**。

## 歧义见证

重开已封存的歧义接受结论时，审查员需要知道两棵稳定派生树**首次在何处作出不同选择**，
而无需人工比对完整树。`GET /api/v1/witness/<audit_id>` 在既有按审计标识读取流程中返回：

- `shared_prefix`：两树共享的最长结构前缀（共同经过的非终结符节点：符号、产生式编号、跨度）；
- `divergence`：首次分歧的非终结符 `symbol` 与两侧词元跨度 `span_first` / `span_second`，
  以及两侧采用的 `production` 和各自后续 `subtree_summary`
  （符号/跨度/产生式/先序产生式序列/逐层子节点，终结符叶保留词元与跨度）。

对齐规则与限制：

- 对齐**仅依据派生节点的非终结符符号与输入跨度 `[i,j)` 推进**，不比较先序产生式编号。
  因此空右部产生式以零跨度节点 `[i,i)` 参与对齐；同一产生式编号在不同跨度出现时不会误配；
  左递归展开按其实际消费跨度定位到可复核的真实节点（如共享外层 `S[0,2]`，分歧在内层 `S[0,1]`）。
- 见证**只从已封存的 first/second 两棵有限树重新导出**，不重新解析输入，
  因而重启后与原结论保持一致（重复读取幂等）。
- 对 `UNIQUE_ACCEPTED` 或 `REJECTED` 结论请求该视图：HTTP 200，`witness_available: false`
  并以中文 `detail` 明确说明不存在歧义见证，**绝不生成伪证据**。
- 未知审计标识沿用既有读取语义：HTTP 404 `NOT_FOUND`。

## HTTP

`POST /api/v1/analyze`

```json
{
  "audit_id": "mission-001",
  "nonterminals": ["E"],
  "productions": [
    {"id": 1, "lhs": "E", "rhs": ["E", "+", "E"]},
    {"id": 2, "lhs": "E", "rhs": ["E", "*", "E"]},
    {"id": 3, "lhs": "E", "rhs": ["id"]}
  ],
  "start": "E",
  "tokens": ["id", "+", "id", "*", "id"]
}
```

- `GET /healthz` 健康响应；`GET /api/v1/conclusion/<audit_id>` 取回封存结论；
  `GET /api/v1/witness/<audit_id>` 查询歧义见证（见下）
- 端口经环境变量配置：`ARBITER_HOST`（默认 `0.0.0.0`）、`ARBITER_PORT`（默认 `8080`）、
  `ARBITER_STORE`（默认 `/data/sealed.json`）
- 纯 Python 标准库实现，无运行时第三方依赖

## Compose 验收

验收以名为 `verify` 的服务**一次执行后退出并返回状态码**（全通过 0，任一失败 1）。
推荐用 `run`，其退出码即验收状态码：

```bash
# 自动先构建镜像、等待 arbiter 健康，再在 verify 内执行：
# 单元测试 -> 显式镜像构建 -> 唯一/歧义/无消费环（含回放、冲突）HTTP 冒烟
docker compose run --rm --build verify
echo "verify 退出码：$?"
```

或整组启动（较新 Compose 可在 verify 失败时一并中止）：

```bash
ARBITER_HOST_PORT=9090 docker compose up --build --abort-on-container-failure verify
docker compose down -v   # 清理
```

宿主端口经 `ARBITER_HOST_PORT` 配置（默认映射 `8080:8080`）。

## 本地开发与测试

```bash
python3 -m unittest discover -s tests -v       # 52 项单元测试
python3 -m app.service                          # 直接启动服务
ALLOW_LOCAL_FALLBACK=1 bash scripts/entrypoint.sh  # 无 Docker 时本地完整验收
```

## 目录

```
app/grammar.py    请求结构校验（限制/非法符号/悬空引用/首个原因）
app/engine.py     静态分析（可生成性、不消费词元循环）+ Earley/SPPF 构建 + 稳定选树
app/storage.py    语义指纹、封存、回放、冲突保留
app/service.py    HTTP 服务
tests/            引擎/歧义见证/封存/HTTP 单元测试
scripts/          verify.py（冒烟）与 entrypoint.sh（验收编排）
Dockerfile        仲裁服务镜像
Dockerfile.verify 验收镜像（Python + 静态 docker CLI）
docker-compose.yml
```
