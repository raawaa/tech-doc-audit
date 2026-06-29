# ADR-0001: 统一 agent loop 与 LLMStep adapter 接口（Path B）

- **状态**: Accepted
- **日期**: 2026-06-29
- **关联**: issue #13（epic）、#15（本决策归属）、#14（预重构，已合并 PR #17）、#16（qa 流式迁移，③）

## 背景 (Context)

三个 agent loop 各自维护一套近乎相同的控制流骨架（迭代 / cancel-check / issue 事件发射 / max-turns / 后处理 / trace），仅 **LLM 调用点**真正分叉：

| loop | 位置 | LLM 调用 | 消息表示 | 终止信号 | 事件发射时机 |
|------|------|---------|---------|---------|------------|
| native function calling | `agentic_audit._run_native_tool_calling` | 非流式 `create(tools, thinking)` | `list[dict]` | `not tool_calls` | 调用后一次性 `reasoning` |
| structured_llm | `agentic_audit._run_structured_llm_loop` | `as_structured_llm(AgentAction).chat()` | LlamaIndex `ChatMessage` | `action=="finish"` | 调用后 `reasoning`(=thought) |
| qa 流式 | `agentic_qa.run_agentic_qa` | 流式 `create(stream=True, …)` | `list[dict]` | `not tool_calls` | 调用**期间** `reasoning_start/delta/end` + `text_start/delta/end` |

**关键 crux**（来自 #13）：三 loop 在 LLM 调用点分叉，且 **qa 在调用期间发事件**（流式）。故 adapter 必须 **own 每次 LLM 调用 + 其间的事件发射**，不能只「返回结果」——否则统一 loop 在 ③（qa 迁移）时必须为流式重新打开 loop 主体。

`_make_emitter` / `_check_cancelled`（#14，已合并）已先行抽出两段逐字重复的辅助，为本次接口决策铺路。

## 决策 (Decision)

经 `/design-an-interface` design-it-twice 比 3 个候选后，采用 **候选 A**：一个方法 + 一个小型判别联合的**请求/响应** adapter，作为统一 `run_agent_loop(...)` 的 seam。

```python
class LLMStep(Protocol):
    def step(self, messages: list[dict], emit: Callable[[dict], None]) -> StepResult: ...

@dataclass
class Final:
    answer: str                       # 最终文本（msg.content / final_summary）
@dataclass
class ToolCalls:
    calls: list[dict]                 # [{"name":..., "args":..., "id":...}]，loop 无关的归一形态
StepResult = Final | ToolCalls
```

**职责划分：**

- **adapter（`LLMStep` 实现）own** —— 单次 LLM 调用 + 其事件发射（调用期间或之后，由 adapter 自决）；消息表示转换（`ChatMessage`↔`dict`）；终止信号归一（`not tool_calls` / `action=="finish"` → 返回 `Final`）；parse-fallback 阶梯；流式 chunk 累积。
- **`run_agent_loop` own** —— 迭代、cancel-check（复用 #14 helper）、工具分发、`issue_found` 发射、max-turns、`save_trace`、`link_standards`、结果构造（`_build_result` / `{answer, sources}`）。

**借自候选 B**：把 loop 的发散回调（`emit` / `dispatch` / `check_cancelled`）打包成一个小上下文对象注入，避免 `run_agent_loop` 签名膨胀（否则 ~8 个参数）。

### 流式契约（为 ③ 留口，本 ADR 钉死）

`StreamingLLMStep.step()` 在调用期间 `emit({"type":"reasoning_delta"/"text_delta", ...})` 增量发射，结束后返回与批式 adapter **相同的 `StepResult`**。**`run_agent_loop` 的主体对批式与流式逐字节相同**——无 `if streaming:` 分支。故 ③（qa 迁移）只需实现一个流式 adapter，无需重新打开统一 loop。audit 无须「假装」流式：它仍只发一次批式 `reasoning`。

## 后果 (Consequences)

**正向：**

- 删除 ~120–160 行三重复制的 loop 骨架；每个 adapter 仅保留「调用 + 发射 + 结果分类」约 40–60 行。
- ③ 无须重新打开 loop。
- loop 可用 fake `LLMStep`（返回脚本化 `StepResult` 序列）单测 cancel / max-turns / issue 发射 / trace，无需模型。

**代价 / 已知 leak：**

- **消息货币钉死 `list[dict]`** —— structured adapter 每轮需 `ChatMessage`↔`dict` 转换（仅 fallback 路径，成本局部）。
- **`issue_found` 检测留在 loop** —— issues 存于 loop 的共享列表，loop 须窥探分发结果判断是否新增问题；这是 loop 唯一不「全盲」之处（审核域固有，非缺陷）。
- **工具调用归一** —— adapter 须把各自的 tool-call 形态（OpenAI `tc.function.arguments` JSON 串 / `AgentAction` 字段映射）归一为 `{name, args, id}`。
- **native 路径无法 fake-model 单测** —— OpenAI client 难干净伪造，仍需真模型 smoke（固有，非本形状引入）；structured 可 mock `get_llm()`。

## 备选方案 (Alternatives considered)

design-it-twice 共比 3 个候选：

- **B（环境注入 / 控制反转，流式优先）** —— adapter 当主角、loop 当薄驱动，向 adapter 注入 `AgentEnvironment{emit, dispatch, check_cancelled, trace_sink, budget}`。流式最干净，但 adapter 合约**更重**（须感知 dispatch / cancel / budget + 事件词汇），偏离现有代码较大，对「2 审核loop + 1 qa」属过度设计。→ 仅借鉴其「回调打包成上下文对象」一点。
- **C（generator / pull-based）** —— `step()` 为生成器，`yield` 事件、`return` `StepResult`。把「调用期间发」与「调用后发」统一为 yield，最优雅；但 generator 控制流对本代码库是**新引入**（当前零生成器），cancel 变 `gen.close()`、max-turns 挂在 `next()` 边界，可读性 / 调试成本高，而 3 个 loop 中仅 1 个流式，收益不抵成本。→ 作为**文档化 fallback**：仅当 ③ 日后需要比「`step()` 期间 emit」更细的调用内控制时再重启评估（当前不需要）。

## 决策依据小结

候选 A 是最浅迁移风险、最高保真、最深的 seam，以最低成本回答了 #13 的 crux「adapter 是否干净到值得统一」——答案是**值得**。现有 native / structured / qa 的 loop 体几乎可逐行映射到 `step()`，统一 loop 主体即今日三处共享的骨架（迭代 / cancel / 分发 / issue / trace / link / 结果）。
