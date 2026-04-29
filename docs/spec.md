# Minimal Agent Workflow Spec v0.6

Status: **draft for review**.
Origin: v0.5 (Huailu) + v0.6 fills (5 must, 4 small, 3 confirms).
Principles: **local first, stability first, simple first**.

---

## 1. 核心设计原则

- DAG 表达依赖（`needs`），runner 串行执行
- 不支持 `next` / `goto` / 并行
- 支持 fan-in / fan-out（在串行下退化为声明顺序）
- `retry` 是唯一回退机制
- 所有 node output 统一 envelope
- trace 记录每一步执行原因

> DAG for dependency modeling, serial runner for execution.

---

## 2. 五种 Component

| Component | 角色 |
|---|---|
| Workflow | 一次完整任务（goal + state schema + nodes） |
| Node | 最小执行单元（声明依赖、调用、输入、输出格式、verify、retry） |
| Skill / Agent / Tool | `uses` 指向的实际执行者 |
| Verify | Node 内部检查（schema / rule / agent） |
| Runner | 解析 DAG、选 ready node、串行执行、写 output / trace |

---

## 3. Workflow 格式

```yaml
workflow: fix_error
version: 0.6

goal: |
  Diagnose the provided error, propose a minimal safe fix,
  validate the fix, and summarize the result.

state:
  error:
    type: string
    required: true
  max_attempts:
    type: integer
    default: 3

nodes:
  - id: analyze
    goal: Identify the root cause.
    uses: skill.analyze
    input:
      error: "{{state.error}}"
    output_schema:
      root_cause: string
      evidence: array
      confidence: number

  - id: fix
    goal: Produce a minimal patch.
    needs: [analyze]
    uses: skill.fix
    input:
      error: "{{state.error}}"
      root_cause: "{{nodes.analyze.latest.output.data.root_cause}}"
      feedback: "{{retry.feedback?}}"
    output_schema:
      patch: string
      explanation: string
    verify:
      - type: schema
      - type: rule
        assert: "output.data.patch != ''"

  - id: test
    goal: Validate the patch.
    needs: [fix]
    uses: skill.test
    input:
      patch: "{{nodes.fix.latest.output.data.patch}}"
    output_schema:
      passed: boolean
      feedback: string
      test_log: string
    retry:
      target: fix
      until: "nodes.test.latest.output.data.passed == true"
      max_attempts: "{{state.max_attempts}}"
      feedback: "{{nodes.test.latest.output.data.feedback}}"

  - id: summarize_success
    goal: Summarize successful solution.
    needs: [analyze, fix, test]
    when: "nodes.test.latest.output.data.passed == true"
    uses: skill.summarize
    input:
      root_cause: "{{nodes.analyze.latest.output.data.root_cause}}"
      patch: "{{nodes.fix.latest.output.data.patch}}"

  - id: summarize_failure
    goal: Summarize why the workflow failed.
    needs: [analyze, fix, test]
    when: "nodes.test.latest.output.data.passed == false"
    uses: skill.summarize
    input:
      message: "Failed after max attempts"
      last_feedback: "{{nodes.test.latest.output.data.feedback}}"
```

---

## 4. Node 格式

```yaml
- id: string                # required, unique
  goal: string              # optional, short purpose
  needs: [node_id]          # optional
  when: expression          # optional, evaluated when ready
  uses: string              # required, "skill.X" / "agent.X" / "tool.X"
  input: object             # optional, template-rendered
  output_schema: object     # recommended; auto-checked by runner after every success
  verify: [verify_rule]     # optional, EXTRAS only (rules / file / command / agent)
  retry: retry_policy       # optional, see §5
```

**Auto-schema 规则**：节点跑 success 之后，runner **自动**对 `data` 做 schema 检查（每个 `output_schema` 字段必须出现在 `data` 里）。Schema 失败 → 节点状态翻 failure → 进入 retry / halt 路径。

→ **用户在 `verify:` 里不再需要写 `{type: schema}`**。仍允许写（兼容老用法），但是 no-op。

→ `verify:` 字段只用于"schema 之外的额外检查"——`type: rule` 表达式断言、`type: agent`（v0.7+）、未来的 `type: file` / `type: command`。

v0.6 故意**不**收录的字段：`timeout` / `allowed_tools` / `model`。
- timeout 由 runner 全局控制 + retry.max_attempts 兜底
- allowed_tools 是 skill 层职责
- model 写进 skill 定义里

---

## 5. Retry 格式

```yaml
retry:
  until: <expression>                # required, success condition
  max_attempts: <int or template>    # required
  feedback: <expression or template> # optional, exposed to next attempt as {{retry.feedback}}
```

**语义（v0.6 简化模型）**：

1. **retry 只重跑当前节点。** 没有 `target` 字段。要影响多个节点的重跑，把它们合并成一个 skill / agent。
2. 触发条件（任一）：
   - 节点 `status: success` 但 `until` 为 false
   - 节点 `status: failure`（节点崩了或 verify 失败）—— retry 自动尝试自我修复
3. 每次 retry 在当前节点上生成新 attempt（per-node 计数，从 1 起）。历史 attempts 全保留，可通过 `nodes.<id>.attempts[n]` 访问。
4. `max_attempts` 是**当前节点**的 attempt 总数上限。
5. 达到 `max_attempts` 仍未达标 → 触发 `retry_exhausted` + `workflow_halted`，等 human/orchestrator 介入（见 §16）。
6. `feedback` 在每次 retry 触发时 render，然后作为 `{{retry.feedback}}` 注入下次 attempt 的 input 模板。

**Trace 示例（test 节点自我重试 3 次后通过）**：

```
analyze#1
fix#1
test#1 (passed=false → retry self with feedback)
test#2 (passed=false → retry self with feedback)
test#3 (passed=true)
summarize#1
```

**Trace 示例（retry 用尽 → halt）**：

```
collect#1
validate#1 (ok=false → retry with feedback)
validate#2 (ok=false → retry_exhausted, max_attempts=2)
node_skipped: publish (upstream_halted)
workflow_halted
```

---

## 6. Output Envelope

所有 node attempt 必须产出统一 envelope：

```yaml
output:
  status: success | failure | skipped | halted
  data: {}        # 业务产物，符合 output_schema
  error: null     # 失败时填
  metrics: {}     # 可选数值指标
  artifacts: []   # 可选大文件引用
```

**`status: halted`** 用于 skill / tool 主动表示"我尽力了，需要外部介入"。runtime 看到这个会立刻 halt 整个 workflow（不重试，不继续），见 §16。

**`status: skipped`** 用于 `when: false` 的节点。Skipped envelope 形如：

```yaml
output:
  status: skipped
  data: {}
  error: null
  metrics: {}
  artifacts: []
```

Skipped 节点也写 output（保证 `nodes.X.latest.output` 引用永远合法）。

**失败 envelope**：

```yaml
output:
  status: failure
  data:
    feedback: "Unit test test_parser_null_input failed"   # 可选反馈
  error:
    code: TEST_FAILED
    message: "Validation failed"
    details: { ... }
  artifacts:
    - name: test.log
      uri: "artifact://run-123/test/1/test.log"
      type: text/plain
```

---

## 7. Data 规则

- `data` 必须符合 `output_schema`
- `data` 只放结构化小数据
- 大文件放 `artifacts`
- 错误信息优先 `error`；可执行反馈可放 `data.feedback`

---

## 8. State 格式

State 是 workflow 级共享变量。

**Schema 声明**（workflow 顶层）：

```yaml
state:
  error:
    type: string
    required: true
  max_attempts:
    type: integer
    default: 3
```

**运行时值**（注入自外部，例如 CLI 参数）：

```yaml
state:
  error: "Build failed with linker error"
  max_attempts: 3
```

**v0.6 规则**（**变化点**）：

- State 在 workflow 启动时注入，**启动后只读**。
- Node 不能写 state；node 之间共享数据用 `nodes.<id>.latest.output` 而不是 state。
- 这意味着旧 DSL 的 `set:` 字段**彻底废弃**。
- State 只存"workflow 启动时已知的常量"（错误描述、配额、外部传入参数）。

---

## 9. Trace 管理

Trace 是 runner 每一步的事实记录，写到 `.camflow/runs/<run_id>/trace.jsonl`，每行一个 event。

**事件 schema**：

```yaml
event:
  step: integer            # 全局递增
  ts: ISO8601
  event: <event_type>
  node: string | null
  attempt: integer | null  # per-node, 1-indexed
  status: string | null
  reason: string           # human-readable why
  extra: {}                # event-specific fields
```

**事件类型（穷举）**：

```
workflow_started
node_ready
node_started
node_completed
node_failed
node_skipped
node_halted          # 节点 envelope status=halted，或失败无 retry
verify_started
verify_completed
verify_failed
retry_triggered
retry_exhausted
workflow_completed   # 全绿
workflow_halted      # 任何节点 halt → 等 resume
workflow_failed      # 死锁 / 表达式异常 / 不可恢复
```

---

## 10. Runner 选择规则

每一轮：

1. 找出所有 ready nodes：
   - 该 node 还没被执行过 OR 被 retry 标记需要重跑
   - 所有 `needs` 节点 status 都是 `success` 或 `skipped`
2. 对每个 ready node 评估 `when`（缺省视为 true）。
   - `when` 为 false → 该 node 立即写入 skipped envelope，不执行 body
   - `when` 为 true → 进入候选列表
3. **多个候选时，按 `nodes:` 声明顺序选第一个**（确定性 tiebreak）。
4. 串行执行选中 node。完成后写 output + trace，回到第 1 步。
5. 没有 ready node 时：
   - 所有 node 都终态（success / failure / skipped） → workflow 终态（见 §16）
   - 否则视为 deadlock → workflow_failed (extra.reason = "no ready nodes")

**Fan-in/fan-out 例子**：

```
A → B → D
 ↘ C ↗
```

执行顺序：`A → B → C → D`（B 在 C 之前，因为先声明）。

---

## 11. Prompt Compiler

Node 不写完整 prompt。Compiler 拼装：

```
[system]
Workflow goal: {{workflow.goal}}
Node goal:     {{node.goal}}

[user]
{{skill.template, rendered with input + retry context}}

Return a JSON object matching this schema:
{{output_schema}}
```

**约定**：
- `output_schema` 由 compiler 自动追加到 user prompt 末尾，skill template **不要**自己写
- skill template 通过 `{{input.X}}` 访问 node input；通过 `{{retry.feedback}}` 访问 retry 反馈
- compiler 只是字符串拼装，不做 LLM 调用

Skill 定义形如（落到磁盘）：

```yaml
# skills/fix.yaml
id: skill.fix
template: |
  Error:
  {{input.error}}

  Root cause:
  {{input.root_cause}}

  {%- if retry.feedback %}
  Previous attempt feedback:
  {{retry.feedback}}
  {%- endif %}

  Generate a minimal safe fix.
```

---

## 12. 最终一句话定义

> 这个 spec 是一个支持 DAG 依赖的串行 Agent Workflow：`needs` 管依赖，runner 管顺序，`output` 管结果，`state` 管启动期共享变量，`trace` 管可观测性，`retry` 是唯一受控回退机制。

---

# 附录 A — Expression Grammar

`when`、`verify[].assert`、`retry.until`、`retry.feedback`（如果是表达式形式）使用同一个表达式语言。

**v0.6 子集（最小够用）**：

| 类别 | 支持 |
|---|---|
| 字面量 | string (单/双引号), int, float, bool, null |
| 标识符 | `state`, `nodes`, `inputs`, `output`, `retry` 根；任意 `.field` 链；任意 `[n]` 下标 |
| 比较 | `==` `!=` `<` `<=` `>` `>=` |
| 布尔 | `and` `or` `not` |
| 括号 | `(...)` |
| 可空标记 | 标识符末尾 `?` 表示"不存在时返回空字符串"，**只在 input 模板里允许** |

**不支持**（v0.6 故意省）：算术、字符串拼接、函数调用、列表/字典字面量、ternary。

**实现策略**：用 Python `ast` 解析 + 白名单 walk，**不用** `eval`。约 50 行 Python。

---

# 附录 B — Template Namespaces

模板 `{{...}}` 在 4 个地方出现：node `input`、`when`、`verify[].assert`、`retry.*`。

可见的命名空间：

| 命名空间 | 内容 | 何时可见 |
|---|---|---|
| `state.X` | workflow 启动时注入的只读 state | 始终 |
| `nodes.X.latest.output.*` | X 的最新 attempt 的 envelope | X 已执行过 |
| `nodes.X.attempts[n].output.*` | X 的第 n 次 attempt（n 从 1 起） | X 至少跑过 n 次 |
| `output.*` | 当前节点本次 attempt 的 envelope | **只在 verify[].assert 中可见** |
| `retry.feedback` / `retry.attempt` | retry 上下文 | **只在被 retry 重跑的 target node input 中可见** |

`{{retry.feedback?}}` 的 `?` 让模板在第一次执行（非 retry）时安全回退到空字符串。

---

# 附录 C — Local Artifact Storage

v0.6 只支持本地文件系统。

**路径布局**：

```
<project>/.camflow/runs/<run_id>/
  ├── workflow.yaml             # 当时的 workflow 快照
  ├── state.json                # 启动时注入的 state
  ├── trace.jsonl               # 所有 event
  ├── halt.json                 # 仅在 halted 时写：halted_node + reason + envelope
  ├── runner.pid                # runner 运行期间存在；正常退出/halt 后清理
  ├── nodes/
  │   └── <node_id>/
  │       └── attempt-<n>/
  │           ├── output.json   # 节点的 envelope（runner-managed）
  │           └── workspace/    # agent 的工作目录（节点视角）
  │               ├── input.json     # 渲染后的 inputs
  │               ├── prompt.txt     # 编译好的 prompt（skill / agent only）
  │               ├── response.txt   # LLM 原始回答（skill / agent only）
  │               ├── raw_stdout.txt # tool 原始 stdout (tool only)
  │               ├── raw_stderr.txt # tool 原始 stderr (tool only)
  │               └── <agent-created files>   # agent.X 在 v0.8 自由写
```

`workspace/` 是节点的"工作区"——所有 agent / tool 看到的都在这里：
- 工具子进程的 cwd 设为 `workspace/`，并通过 `CAMFLOW_WORKSPACE` 环境变量暴露绝对路径
- skill / agent 的 prompt + inputs 落到 workspace/，方便 debug 也方便未来传给 camc agent
- agent.X (v0.8) 通过 camc spawn 时，camc 的 cwd 会是 `workspace/`，agent 写出来的文件就留在这里

**Artifact URI 解析**：`artifact://<run_id>/<node>/<attempt>/<name>` →
`<project>/.camflow/runs/<run_id>/nodes/<node>/attempt-<attempt>/workspace/<name>`

`<run_id>` 由 runner 启动时生成（建议 ISO 时间戳 + 短随机：`20260428-173812-a1b2`）。

---

# 附录 D — Failure & Skip Propagation

| 触发 | 行为 |
|---|---|
| node status=success 但 `until` 为 false（有 retry） | 触发 retry，**重跑当前节点** |
| node status=failure（有 retry） | 触发 retry，**重跑当前节点** |
| node retry max_attempts 用完 | `retry_exhausted` + **`workflow_halted`**：所有未执行节点写 skipped envelope（`code: UPSTREAM_HALTED`），写 `halt.json`，runner 退出（exit 2） |
| node status=failure 且**无** retry | 直接 **`workflow_halted`**（不是 failed）：让 human/orchestrator 看一眼，可能 resume |
| node status=halted（skill/tool 主动返回） | 立即 **`workflow_halted`**：halt.json 记下 envelope，下游 skipped |
| `when` 为 false | 节点写 skipped envelope，不执行 body，下游 needs 视它为已满足 |
| 上游 node skipped | 下游照常评估 needs（skipped 算"满足"），但若该上游是核心数据来源，下游 input 模板渲染时会拿到 skipped envelope —— 由 node 的 `when` 自己处理这种情况 |

**全局终态 + exit code**：

| 终态 | 条件 | exit |
|---|---|---|
| `workflow_completed` (success) | 所有节点 success 或 skipped | 0 |
| `workflow_halted` | 任何节点 halt 或 retry 耗尽 → 等 resume | **2** |
| `workflow_failed` | 死锁 / 表达式异常 / 不可恢复错误 | 1 |

**halt vs failed 的区别**：halt 是"暂停，可能能恢复"——human/orchestrator 看 trace + halt.json + state.json 后可以编辑 state、改 workflow、或者 resume。failed 是"真坏了，重跑也没用"——比如表达式写错、死锁、内部 bug。

**halt.json**：halted 时在 run dir 顶层写一个 sidecar：

```json
{
  "halted_node": "<node_id>",
  "halted_attempt": <int>,
  "reason": "<human-readable>",
  "envelope": { ... },     // 该节点最后一次 attempt 的 envelope
  "trace_step": <int>      // workflow_halted 事件在 trace 里的 step
}
```

---

# 附录 E — v0.5 → v0.6 变更

| # | 变更 | 位置 |
|---|---|---|
| 1 | 表达式语法明确（最小子集） | 附录 A |
| 2 | 4 个 template namespace + `?` 后缀语义 | 附录 B |
| 3 | **retry 简化为只重跑当前节点；删 `target` 字段** | §5 |
| 4 | 失败传播规则明文化 | 附录 D |
| 5 | 本地 artifact 存储路径约定 | 附录 C |
| 6 | 声明顺序作为 fan-out tiebreak（明文） | §10 |
| 7 | `when: false` 节点写 skipped envelope | §6 / §10 |
| 8 | attempt 计数 per-node、1-indexed | §5 / §9 |
| 9 | `output_schema` 自动追加到 user prompt 末尾 | §11 |
| 10 | state 启动后只读，废除 `set:` | §8 |
| 11 | 移除 timeout / allowed_tools / model 节点字段 | §4 |
| 12 | 默认 run 目录布局：`.camflow/runs/<run_id>/` | 附录 C |
| 13 | **新增 envelope status `halted`** | §6 |
| 14 | **新增 `node_halted` / `workflow_halted` trace 事件** | §9 |
| 15 | **node 失败无 retry → workflow_halted（不是 workflow_failed）** | 附录 D |
| 16 | **retry 耗尽 → workflow_halted；写 `halt.json` sidecar** | §5 / 附录 D |
| 17 | **exit code: 0=success, 2=halted, 1=failed** | 附录 D |
| 18 | **Auto-schema：runner 自动检查 output_schema，verify 里不再需要 `{type: schema}`** | §4 / §6 |
| 19 | **Workspace dir：每个 attempt 一个 `workspace/` 子目录，prompt/input/raw 都进去；tools cwd 设到这里 + `CAMFLOW_WORKSPACE` env var** | 附录 C |
| 20 | **`_build_agent_context()` 抽出成 dedicated context-builder**，skill / tool / (future) agent 共用 | §11 |
