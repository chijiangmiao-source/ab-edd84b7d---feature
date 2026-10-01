# 深空地面站回绕时标审计服务

汇集多个设备的回绕时标（模 `M` 计数器读数），判断带因果窗口的记录能否落在
同一条真实时间线上。**相同计数值不视为同一时刻**——每个事件的绝对时刻为
`absolute = counter + M × wrap`，回绕次数 `wrap` 为非负整数，由约束精确推出。

## 语义模型

- **锚点**：绝对 tick 已知的节点（`anchor.absolute`），其回绕次数固定。
- **事件**：至多 12 个，各给出一个模 `M` 计数值 `counter ∈ [0, M)`。
- **约束**：带标识的闭区间 `source --[lo, hi]--> target`，含义为
  `lo ≤ absolute(target) − absolute(source) ≤ hi`。
- 全部事件必须经约束（无向）连通锚点，否则请求被拒绝（400）。

约束两边减去计数残差后得到回绕次数上的整数差分约束：

```
k_t − k_s ∈ [ ⌈(lo − (c_t − c_s)) / M⌉ ,  ⌊(hi − (c_t − c_s)) / M⌋ ]
```

用最短路/负环机制求解，判定三态：

| 状态 | 含义 | 结论内容 |
|---|---|---|
| `unique` | 唯一时间线 | 每个事件的 `wrap` 与 `absolute` |
| `ambiguous` | 多解 | 按事件标识顺序的**前两条规范时间线**（字典序最小两条）及**首个不稳定先后关系**（标识序上第一对先后顺序不被所有解共享的节点，含两条时间线中各自的关系与可达区间） |
| `unsatisfiable` | 无解 | **可复算的冲突约束链**：负环上的约束标识、每步整数界、权重总和 `< 0` |

参考例：`M=100, A=95, B=3, A→B=[8,8]` 唯一展开为 `B = 3 + 100×1 = 103`。

## 自适应询问计划

审计员打开一条结论为 `ambiguous` 的记录后，可以提交一份**自适应询问计划**：
指定一对必须在现场确认先后的**目标事件** `target`，以及至多 10 个可向设备
询问的**候选事件对** `pairs` 和一个稳定的 `plan_id`。设备每次询问一对事件，
只回答三种结果：

| 回答 | 含义（对有序对 `(a, b)`） |
|---|---|
| `before` | `absolute(a) < absolute(b)` |
| `same` | `absolute(a) = absolute(b)`（仅当两事件计数残量模 `M` 相等才可能） |
| `after` | `absolute(a) > absolute(b)` |

每个回答都被还原为回绕次数上的整数差分约束（`k = wrap(b) − wrap(a)`）：

```
before:  k ≥ ⌊(c_a − c_b) / M⌋ + 1
same:    k = (c_b − c_a) / M      （要求 c_a ≡ c_b (mod M)）
after:   k ≤ ⌈(c_a − c_b) / M⌉ − 1
```

服务从**冻结的原始约束**出发，逐分支用最短路闭包收紧可行集：

- 原本就不可能的回答在该分支被剪枝（计划里列为 `pruned_answers`，不会出现在
  现场流程中）；
- 每个**可达叶**都必须使目标对在该可行集上只剩唯一关系——判定依据是完整
  差分闭包给出的紧界，而不是两条规范时间线或某一条样例路径；
- 计划在「最坏询问数最少」的前提下，再按候选事件对标识序列的字典序裁决，
  返回唯一规范计划，响应中给出 `worst_case_queries`；
- 如果无论怎样自适应询问都无法确定，`status` 为 `undecidable`，并给出
  `counterexample`：一条可复算的回答路径（逐步列出施加的整数界），以及两条
  都满足原始约束与该路径、但目标关系相反的具体时间线。

计划请求的校验与持久化规则：

- 来源审计不是 `ambiguous` → `409`；目标/候选事件不存在、候选对重复（含反向
  重复）、候选对超过 10 个等 → `400`，均不写入半成品；
- 相同 `plan_id` + 相同内容（目标/候选对顺序无关）→ `200` 重放原计划；
  `plan_id` 改换内容 → `409`，不新增记录；
- `GET /plans/{plan_record_id}` 读取计划时**连同来源审计的冻结输入、结论与
  证据**一并返回（`source_audit`）。


## API

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 健康检查 |
| `POST` | `/audits` | 创建审计（对 `request_id` 幂等） |
| `GET` | `/audits` | 列出审计编号与数量 |
| `GET` | `/audits/{id}` | 读取冻结的输入、结论与证据 |
| `POST` | `/audits/{id}/plans` | 针对一条 ambiguous 审计生成自适应询问计划 |
| `GET` | `/audits/{id}/plans` | 列出该审计派生的计划 |
| `GET` | `/plans/{plan_record_id}` | 读取冻结的计划及其来源审计证据 |

### 创建示例

```bash
curl -X POST localhost:${HOST_PORT:-8080}/audits -d '{
  "request_id": "req-1",
  "modulus": 100,
  "anchor": {"id": "A", "absolute": 95},
  "events": [{"id": "B", "counter": 3}],
  "constraints": [{"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8}]
}'
```

返回 `201` 与完整记录（`audit_id`、`status`、`conclusion`、`evidence`）。

### 计划示例

```bash
curl -X POST localhost:${HOST_PORT:-8080}/audits/AUD-000001/plans -d '{
  "plan_id": "plan-on-site-1",
  "target": ["X", "Y"],
  "pairs": [["A", "Z"], ["A", "Y"]]
}'
```

返回 `201` 与自适应树 `tree`：每个询问节点给出 `pair`、仍可达的
`reachable_answers`、被剪枝的 `pruned_answers`，以及按回答分岔的 `branches`
（每支附该回答施加的整数界 `bounds`）；叶节点给出确定的 `target_relation`。
无法判定时 `status` 为 `undecidable` 且 `tree` 为 `null`，`counterexample`
给出回答路径与两条目标关系相反的时间线。

### 幂等语义

- 相同 `request_id` + 相同载荷（事件/约束顺序无关）→ `200`，返回**原审计编号**，
  `replayed: true`；
- 相同 `request_id` + 改换任一事件或约束 → `409`，**不新增记录**；
- 载荷非法 → `400`（`problems` 列出全部问题），不占用 `request_id`。

## 运行

### 本地

```bash
PORT=8080 python3 -m app.server
```

### Docker Compose

```bash
HOST_PORT=9090 docker compose up --build app   # 健康检查走可配置宿主机端口
curl localhost:9090/health
```

## 一次性验收服务 verify

`verify` 是执行后即退出的一次性服务，依次执行：

1. **构建检查**：全部源码编译；
2. **代码测试**：求解器/存储/计划引擎/API 单元测试；
3. **API/HTTP 冒烟**：参考展开（B=103）、歧义双时间线与首个不稳定先后关系、
   双向矛盾链（权重可复算且 `< 0`）、幂等记录（重放/冲突/不新增），以及
   自适应询问计划——可达分支、不可达回答剪枝、最短规范计划、失败反例（同一条
   回答路径上两条目标关系相反的时间线）、拒绝规则与冻结来源读取。

```bash
docker compose up --build --exit-code-from verify --abort-on-container-exit verify
echo $?   # 0 = 验收通过，非 0 = 失败
```

本地等价运行：

```bash
PORT=18080 python3 -m app.server &
APP_URL=http://127.0.0.1:18080 python3 -m verify.verify
```

## 结构

```
app/solver.py         回绕展开与三态判定（纯函数，无第三方依赖）
app/planner.py        自适应询问计划：回答->整数差分界、逐分支闭包、
                      最短规范决策树与 undecidable 反例
app/store.py          幂等审计与计划存储（指纹、冻结记录、计划带来源证据）
app/server.py         HTTP API（stdlib，PORT 环境变量配置端口）
app/healthcheck.py    容器健康检查
tests/                单元测试（63 例）
verify/verify.py      一次性验收（构建检查 + 代码测试 + HTTP 冒烟，含计划场景）
Dockerfile           单一镜像，app  与 verify 共用
compose.yaml         app（健康检查、可配置宿主机端口）+ verify（一次性）
```
