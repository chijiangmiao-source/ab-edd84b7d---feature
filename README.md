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

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 健康检查 |
| `POST` | `/audits` | 创建审计（对 `request_id` 幂等） |
| `GET` | `/audits` | 列出审计编号与数量 |
| `GET` | `/audits/{id}` | 读取冻结的输入、结论与证据 |
| `POST` | `/audits/{id}/plans` | 在某条审计下创建自适应询问计划 |
| `GET` | `/audits/{id}/plans` | 列出该审计下的计划编号 |
| `GET` | `/plans/{ref}` | 读取冻结计划（连同来源审计） |

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

### 幂等语义

- 相同 `request_id` + 相同载荷（事件/约束顺序无关）→ `200`，返回**原审计编号**，
  `replayed: true`；
- 相同 `request_id` + 改换任一事件或约束 → `409`，**不新增记录**；
- 载荷非法 → `400`（`problems` 列出全部问题），不占用 `request_id`。

## 自适应现场询问计划

仅当审计结论为 `ambiguous` 时，审计员可在该审计下提交一个计划：指定一对
**目标事件**（必须在现场确认先后），以及至多 **10 个**可向设备询问的候选
事件对。每次询问只返回三种结果（相对提交对 `[left, right]`）：

- `before`：left 严格先于 right；
- `same`：两者同刻；
- `after`：left 严格后于 right。

回答在冻结的整数回绕差分约束上继续收紧（记
`d = counter(right) − counter(left)`）：

```
before:  wrap(right) − wrap(left) ≥ ⌈(1 − d)/M⌉
after:   wrap(right) − wrap(left) ≤ ⌊(−1 − d)/M⌋
same:    wrap(right) − wrap(left) = −d/M      （仅当 M 整除 d 时可能）
```

服务对每个仍可行（收紧后不出现负环）的回答选择下一次询问，构建一棵有限
自适应决策树：每个**可达叶**都只剩唯一目标关系——仅给出两条规范时间线或
单条路径不算保证；本来不可能的回答被剪枝并列入 `impossible_answers`。
计划按**最坏询问数最少**择优，并列时按候选对标识序列字典序裁决。

返回 `status: "resolved"` 时给出 `tree`（含每节点可达/被剪回答、施加的
整数界与叶节点回答路径）与 `worst_case_questions`。

### 不可区分反例

若用所提交的候选对、在 10 次询问内无论如何都无法确定目标关系，返回
`status: "indistinguishable"` 与 `counterexample`：

- `answer_path`：一条设备可逐问给出的具体回答序列（每步含施加的整数界）；
- `timeline_1` / `timeline_2`：两条**完整时间线**，都满足冻结约束、都对
  路径上每次询问给出完全相同的回答，但目标关系不同；
- `answer_replay`：把两条时间线逐问复算，结果均等于路径回答，可独立核验。

### 计划的拒绝与幂等

- 来源审计不是 `ambiguous` → `409 source_not_ambiguous`；
- 目标或候选事件不存在、候选对重复（含左右互换）、候选 id 重复、超过
  10 个、自成对 → `400`，且**不写入半成品计划**；
- 相同 `plan_id` + 相同内容（目标、候选对，顺序无关）→ `200`，返回原
  `plan_ref`，`replayed: true`；同一 `plan_id` 改换内容（或改挂别的审计）
  → `409 plan_id_conflict`，不新增计划；
- `GET /plans/{ref}` 连同**来源审计的冻结证据**（展开、双时间线、冲突链）
  一并返回。

```bash
curl -X POST localhost:8080/audits/AUD-000001/plans -d '{
  "plan_id": "plan-1",
  "target": ["A", "C"],
  "queries": [{"id": "q-ac", "left": "A", "right": "C"}]
}'
```

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
2. **代码测试**：求解器/存储/API 单元测试；
3. **API/HTTP 冒烟**：参考展开（B=103）、歧义双时间线与首个不稳定先后关系、
   双向矛盾链（权重可复算且 `< 0`）、幂等记录（重放/冲突/不新增），以及
   自适应询问计划（可达分支与不可达回答剪枝、最短规范计划、不可区分
   反例、拒绝与冻结回读）。

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
app/solver.py        回绕展开与三态判定（纯函数，无第三方依赖）
app/planner.py       自适应询问计划：逐分支整数差分收紧、minimax 决策树、
                     最短最坏询问数裁决与不可区分反例
app/store.py         幂等审计/计划存储（指纹、冻结记录、冲突不写入）
app/server.py        HTTP API（stdlib，PORT 环境变量配置端口）
app/healthcheck.py   容器健康检查
tests/               单元测试（59 例）
verify/verify.py     一次性验收（构建检查 + 代码测试 + HTTP 冒烟）
Dockerfile           单一镜像，app 与 verify 共用
compose.yaml         app（健康检查、可配置宿主机端口）+ verify（一次性）
```
