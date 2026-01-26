# AuraWork

面向办公场景的异步并线式 Agent（local-first）。

English version: [`README.md`](./README.md)

AuraWork 把一次办公任务的运行拆成三层：

- **WorkSpec**：澄清后的工作规格（目标、输入/输出、约束、范围、风险策略）
- **Plan**：显式依赖的任务图（DAG），支持并线推进
- **Execute**：带预演与审批的执行过程（产物、变更、决策可回放）

这份 README 关注“面向办公的产品理念与工作模型”，而不是底层实现细节。

---

## 项目状态

- 当前推荐使用 **CLI** 形态推进任务。
- **Web Workspace 仍在开发阶段**：前端交互与流程尚未打磨完成，当前版本不保证可用。
- 处于快速迭代期：数据结构与交互可能会有 breaking changes。

---

## 能力范围（面向办公任务）

### v0 稳定支持（Must）

- **工作区文件整理与归档**：扫描、批量重命名、归档建目录、生成索引清单与整理报告、hash 去重
- **文档型交付物生成（vibe writing）**：从零散输入生成结构化文档初稿，支持反复迭代与版本对比
- **异步推进与可见进度**：有计划、有阶段、有产出；支持中途追加材料/约束

### 目标支持（Should）

- **图片/截图 → 表格化产出**：多模态抽取为主，OCR 可选兜底
- **演示稿/报告落盘（基础格式）**
- **网页调研与汇总（只读为主）**：对比矩阵、证据留存与溯源

### v0 非目标（Non-goals）

- 通用桌面 RPA（任意 GUI 自动化）
- 高复杂 Excel（大量公式/透视表/宏）一次成功自动生成

---

## 核心工作模型

### 1) WorkSpec：先澄清，再执行

AuraWork 要求先把需求落成可执行的 WorkSpec，典型包含：

- 目标与交付物（expected outputs）
- 输入材料（files/urls/notes）
- 约束（风格、模板、截止时间、禁止项）
- 资源范围（工作区根目录、文件类型白名单、域名白名单）
- 风险与审批策略（哪些动作需要审批）
- `intent_items`：澄清后可引用的“行动意图条目”（用于门控与审计对齐）

这些字段不仅用于生成计划，也会参与工具层门控：超出 WorkSpec 范围的路径/文件类型/域名访问会被拒绝或提升到更严格的审批。

### 2) Plan/Execute 分离：规划与执行解耦

AuraWork 将职责拆成两个边界清晰的角色：

- **Planner**：生成/修改 DAG；决定是否采纳 Workers 的 Proposal
- **Workers**：执行单个节点；可以提出 Proposal，但不直接改图、不自行扩权

在这个分工下，“计划”不止是待办清单。每个节点除了描述要做什么，还会带上依赖关系和执行约定（使用哪个执行器、在什么范围内操作、期望产出等），因此同一份计划既能被人阅读，也能直接驱动调度与回放。

在办公场景中，Workers 通常按“执行器类型（archetype/preset）”固定职责与工具边界，例如：

- 文件整理（FileOps）
- 文档生成与改写（Doc）
- 表格提取与汇总（Sheet）
- 网页只读调研与证据留存（Browser Read）
- 验收与校验（Verifier）

对应到“办公任务拆解”，这意味着：

- 能并行的子任务并行推进（减少等待）
- 每个节点都有明确的输入/输出与验收标准（便于定位问题与回放）
- Worker 的输出以“结果 + 产物 + 建议”形式返回，由 Planner 决策下一步

### 3) DAG 并线：显式依赖与可并行推进

任务以 DAG 表达依赖关系，调度器按依赖满足情况派发就绪节点（在并发上限内）。依赖边既用于语义依赖，也用于避免写冲突的显式串行化。

执行过程中，节点可能会返回补充步骤、验证建议或拆分建议；这些建议不会直接改图，而是交给 Planner 决策后，以增量方式更新计划，再继续调度后续节点。

### 4) 自愈循环：节点内部自检与修复

对格式/转换/公式引用等低级但高频的错误，Worker 可以在节点内部执行 **Action → Observe → Correct** 循环，在有限次数内尝试自修复，避免把噪声升级成主流程的失败。

### 5) 结构化中间层：Office 文件先转“可编辑表示”

Office/PDF 更适合先转成结构化中间格式（如 Markdown/JSON，保留标题层级、表格边界、图片位置等），在中间层做提取/修改/预览，再写回原格式交付。

---

## 审批与可控性

### 渐进授权：默认只读，逐级放开

以工作区为权限边界，默认偏保守：

- 低风险动作尽量自动完成（例如只读分析、生成新文件）
- 高风险动作（覆盖/移动/删除/执行命令等）必须进入审批流

### OperationPlan（预演清单）：审批前先把“准备怎么改”讲清楚

对批量变更类动作，先生成可读的“预演清单”（数量、类型聚合、规则摘要、明细入口/diff），再由用户决定执行或取消。

### 审批请求的“上浮”与续跑

委派执行器（Workers）内部不做交互式审批：当它需要使用高风险工具时，会停止在当前节点并返回结构化的审批请求（包含动作摘要、风险说明、必要时的 diff/预览）。主流程把它统一呈现在 CLI/Web，并把运行暂停在可恢复的位置。

一次审批记录可以包含多个待执行的工具调用，用于减少重复确认。

用户批准后，系统会先执行被批准的工具调用，再把执行结果作为恢复提示回注到原来的委派任务中继续推进，避免“批准之后还要重新解释上下文”。

在可用时，系统也可以用一个只做判断、不执行工具的审批代理，基于 WorkSpec + 参数 + 预览信息做一次 `allow / deny / require_user` 的二次决策；能自动判定的就直接继续，只有 `require_user` 才打断用户。

### 不可信输入治理：外部内容永远是“数据”，不是“指令”

外部材料（网页/PDF/第三方文件）可能包含“指令性文本”。AuraWork 的处理原则：

- 外部内容仅用于抽取/摘要/对比/引用与证据留存
- 行动意图只来自 WorkSpec（`intent_items`）
- 高副作用动作需要能对齐到某条意图，并引用对应证据

---

## Skills：办公能力的可复用交付单元

Skills 用于把一类办公交付固化为可复用单元（澄清问题集/模板/工具约束/验收方式/输出结构），用于复用而不是每次从零开始。

设计上，一个技能包（SkillPack）可以包含：

- `clarify_template`：澄清问题与 WorkSpec 补全规则
- `dag_template`：推荐 DAG 模板（节点/依赖/默认执行器）
- `tool_profile`：允许的工具子集与默认审批策略（只能收窄，不可放大）
- `acceptance_profile`：验收方式组合
- `output_profile`：输出格式与落盘位置模板

内置 skills（见 `aura/builtin/skills/`）包括：

- `aura-docx` / `aura-pptx` / `aura-xlsx`：Office 读写与结构化处理
- `aura-pdf`：PDF 抽取与整理
- `agent-browser`：网页只读调研与证据留存（基于 https://github.com/vercel-labs/agent-browser）

---

## 交互形态

- **CLI**：交互式推进任务（支持 `/model`、`/perm`、`/stream`、`/compact`）
- **Web Workspace（开发中）**：目标是查看会话、产出物、审批与任务时间线；当前版本暂不作为可用入口

---

## 后续计划（TODO）

- 完成 Web Workspace：会话管理、事件/时间线回放、产物浏览、审批流 UI、DAG/计划视图
- 扩展办公场景能力：更多 SkillPack（整理归档/文档/表格/调研）与更稳定的中间层读写
- 扩展能力边界：更清晰的工作区初始化（带文件启动）、更细的资源范围约束与可见运行契约
- 强化隔离与安全档位：在现有逻辑隔离基础上，引入更强隔离的执行选项（容器/VM 等）

---

## 快速开始（最少步骤）

### Prerequisites

- Python **3.11+**
- Node.js **18+**（仅 Web 开发时需要）

### Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r web/backend/requirements.txt
```

```bash
# Web（开发中）
cd web/frontend
npm install
```

### Initialize

```bash
python -m aura init .
```

编辑 `.aura/config/models.json`，填好你要用的模型 profile（`base_url/model/api_key` 等）。

### Run

```bash
python -m aura chat
```

Web Workspace（开发中，当前不保证可用；用于参与开发）：

```bash
./web-up.sh
```

---

## Third-party notices

Some built-in skills vendor Office Open XML schema resources and include notices under:

- `aura/builtin/skills/*/ooxml/THIRD_PARTY_NOTICES.md`

Keep these notices when redistributing.

---

## License

MIT. See [`LICENSE`](./LICENSE).
