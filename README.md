# AuraWork

An AI agent for office workflows with WorkSpec → DAG planning → approval-gated execution (local-first).
Focuses on documents (DOCX/PDF), spreadsheets (XLSX/CSV), slides (PPTX), web research, and safe workspace file ops.

Chinese version: [`README.zh.md`](./README.zh.md)

AuraWork models an office task as three layers:

- **WorkSpec**: a clarified work specification (goals, inputs/outputs, constraints, scope, risk policy)
- **Plan**: an explicit-dependency task graph (DAG) that supports parallel execution
- **Execute**: execution with previews and approvals (artifacts, changes, and decisions are replayable)

This README focuses on the product model and workflow, not the low-level implementation details.

---

## Project status

- The recommended entry point today is the **CLI**.
- **Web Workspace is still under development**: the frontend UX and end-to-end flow are not finalized, and the current version is not guaranteed to be usable.
- Rapid iteration: data structures and interactions may have breaking changes.

---

## Capability scope (office tasks)

### Stable in v0 (Must)

- **Workspace file organization and archiving**: scanning, batch renaming, foldering/archiving, generating an index + cleanup report, hash-based dedup
- **Document deliverables (“vibe writing”)**: turn scattered input into a structured first draft, with iterative editing and version diffs
- **Async progress with visibility**: plan/phase/output driven; supports adding materials/constraints mid-run

### Target in v0 (Should)

- **Images/screenshots → tabular outputs**: multimodal extraction first, OCR as an optional fallback
- **Slide/report file outputs (basic formatting)**
- **Web research and summarization (read-mostly)**: comparison matrix, evidence retention, provenance

### Non-goals in v0 (Non-goals)

- General-purpose desktop RPA (arbitrary GUI automation)
- “One-shot” generation of complex Excel workbooks (heavy formulas/pivots/macros)

---

## Core workflow model

### 1) WorkSpec: clarify first, then execute

AuraWork expects a task to be captured as an executable WorkSpec, typically including:

- Goals and deliverables (expected outputs)
- Input materials (files/urls/notes)
- Constraints (style, templates, deadlines, forbidden items)
- Resource scope (workspace roots, file-type allowlist, domain allowlist)
- Risk/approval policy (what must be approved)
- `intent_items`: clarified, referencable intent statements (used for gating and audit alignment)

These fields are not only used to generate the plan; they are also used in tool-level gating: out-of-scope paths/file types/domains are denied or escalated to stricter approval.

### 2) Plan/Execute separation: decouple planning from execution

AuraWork splits responsibilities into two clear roles:

- **Planner**: creates/updates the DAG; decides whether to accept proposals from Workers
- **Workers**: execute a single node; can propose changes, but cannot mutate the plan or self-escalate permissions

With this split, a “plan” is not just a to-do list. Each node carries dependencies and an execution contract (which worker preset to use, allowed scope, expected outputs, etc.), so the same plan is both human-readable and directly runnable/replayable.

In office workflows, Workers are typically mapped to fixed “executor types” (archetype/preset), for example:

- File operations (FileOps)
- Document drafting and rewriting (Doc)
- Spreadsheet extraction and aggregation (Sheet)
- Read-only web research with evidence capture (Browser Read)
- Verification and checks (Verifier)

In practice, this means:

- Tasks that can run in parallel do run in parallel (less waiting)
- Each node has explicit inputs/outputs and acceptance criteria (easier debugging and replay)
- A Worker returns “result + artifacts + proposals”; the Planner decides the next step

### 3) DAG parallelism: explicit dependencies, parallel dispatch

Tasks are expressed as a DAG; the scheduler dispatches ready nodes within a concurrency cap. Dependency edges cover semantic prerequisites and also deliberate serialization to avoid write conflicts.

During execution, a node may return additional steps, validation suggestions, or splitting suggestions. These do not mutate the graph directly; they are routed back to the Planner, applied as an incremental plan update, and then scheduling continues.

### 4) Self-healing loops: localize low-level errors inside a node

For frequent low-level issues (format conversion errors, formula/reference mistakes, etc.), a Worker can run an internal **Action → Observe → Correct** loop with a bounded number of retries, so noise doesn’t automatically escalate into a top-level failure.

### 5) Structured intermediate format: edit Office files via an intermediate representation

Office/PDF files are often better handled via a structured intermediate representation (e.g., Markdown/JSON that preserves heading levels, table boundaries, image positions). AuraWork prefers extracting/editing/previewing in that layer and writing back to the original format for delivery.

---

## Approvals and control

### Progressive authorization: default read-only, open up in steps

The workspace is the primary permission boundary:

- Low-risk actions should complete automatically (e.g., analysis, generating new files)
- High-risk actions (overwrite/move/delete/execute commands) must go through approvals

### OperationPlan (preview): show what will change before doing it

For batch changes, generate a readable preview (“OperationPlan”) first: counts, breakdown by operation type, rule summaries, and a details entry (diff/preview). The user then decides whether to proceed or cancel.

### Surfacing approval pauses and resuming work

Delegated Workers do not run interactive approvals internally. When a Worker needs a high-risk tool, it stops at the node boundary and returns a structured approval request (action summary, risk notes, and diff/preview when relevant). The main flow presents it uniformly in CLI/Web and pauses the run at a resumable point.

A single approval record can include multiple pending tool calls to reduce repeated confirmations.

After approval, the system executes the approved tool calls first and then injects the outcomes back into the original delegated task as a resume hint so it can continue, instead of forcing the user to re-explain context.

When available, the system can also run an “approval agent” that only judges (no tool execution) using WorkSpec + arguments + preview, producing `allow / deny / require_user`. Only `require_user` should interrupt the user.

### Untrusted input governance: external content is data, not instructions

External materials (web pages, PDFs, third-party files) may contain instruction-like text. AuraWork treats them as data:

- Use external content for extraction/summarization/comparison/citations/evidence only
- Action intent comes from WorkSpec (`intent_items`), not from external text
- High side-effect actions must map to an intent and cite relevant evidence

---

## Skills: reusable office deliverable units

Skills package an office deliverable into a reusable unit (clarification questions, templates, tool constraints, acceptance checks, output structure), so you can reuse a workflow instead of starting from scratch every time.

By design, a SkillPack can include:

- `clarify_template`: clarification questions and WorkSpec completion rules
- `dag_template`: a recommended DAG template (nodes/deps/default executors)
- `tool_profile`: an allowed tool subset and default approval policy (can only narrow, never broaden)
- `acceptance_profile`: acceptance/check combinations
- `output_profile`: output formats and output path templates

Built-in skills (see `aura/builtin/skills/`) include:

- `aura-docx` / `aura-pptx` / `aura-xlsx`: Office read/write and structured processing
- `aura-pdf`: PDF extraction and organization
- `agent-browser`: read-only web research and evidence capture (built on https://github.com/vercel-labs/agent-browser)

---

## Interaction modes

- **CLI**: interactive task execution (supports `/model`, `/perm`, `/stream`, `/compact`)
- **Web Workspace (in development)**: intended for sessions, artifacts, approvals, and a task timeline; not a stable entry point yet

---

## Roadmap (TODO)

- Finish Web Workspace: session management, event/timeline replay, artifact browsing, approvals UI, DAG/plan views
- Expand office capabilities: more SkillPacks (cleanup/docs/sheets/research) and a more robust intermediate-format I/O layer
- Expand the operational boundary: clearer “workspace bootstrap” (start-with-files), tighter resource-scope constraints, and a visible run contract
- Strengthen isolation and safety profiles: add stronger execution isolation options (container/VM) beyond the current logical isolation baseline

---

## Quick start (minimal)

### Prerequisites

- Python **3.11+**
- Node.js **18+** (only needed for web development)

### Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r web/backend/requirements.txt
```

```bash
# Web (in development)
cd web/frontend
npm install
```

### Initialize

```bash
python -m aura init .
```

Edit `.aura/config/models.json` and fill in the model profile you want to use (`base_url/model/api_key`, etc.).

### Run

```bash
python -m aura chat
```

Web Workspace (in development; not guaranteed to work; for development only):

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
