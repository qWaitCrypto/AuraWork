# AuraWork

[![CI](https://github.com/qWaitCrypto/AuraWork/actions/workflows/ci.yml/badge.svg)](https://github.com/qWaitCrypto/AuraWork/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**A local-first multi-agent framework for office workflows** — clarify requirements, plan as a DAG, execute with parallel subagents and human-in-the-loop approvals.

Chinese version: [`README.zh.md`](./README.zh.md)

### Why AuraWork?

- **Structured before execution** — every task starts with a clarified WorkSpec, not a raw prompt
- **DAG-based parallel dispatch** — dependency-aware scheduling with concurrent subagents
- **Human-in-the-loop approvals** — high-risk actions pause for review; low-risk actions proceed automatically
- **Typed result contracts** — Pydantic-validated subagent results, not ad-hoc JSON parsing
- **Office-native skills** — built-in Word, Excel, PowerPoint, PDF, and browser research capabilities

---

## Architecture

```
                         ┌─────────────┐
                         │   User CLI  │
                         │   / Web UI  │
                         └──────┬──────┘
                                │
                         ┌──────▼──────┐
                         │   Engine    │  Orchestrates the full lifecycle
                         └──────┬──────┘
                                │
              ┌─────────────────┼─────────────────┐
              │                 │                  │
       ┌──────▼──────┐  ┌──────▼──────┐  ┌───────▼───────┐
       │  WorkSpec   │  │   Planner   │  │   Executor    │
       │  Clarify    │  │  (DAG plan) │  │  (Dispatch)   │
       └─────────────┘  └──────┬──────┘  └───────┬───────┘
                               │                  │
                        ┌──────▼──────┐    ┌──────▼──────┐
                        │  Scheduler  │    │  Subagents  │
                        │ (stateless) │    │ (parallel)  │
                        └─────────────┘    └──────┬──────┘
                                                  │
                         ┌────────┬────────┬──────┴───┐
                         │        │        │          │
                      FileOps   Doc    Sheet    Browser
                      Worker   Worker  Worker   Worker
```

---

## Screenshots

![Web Workspace Overview](docs/screenshot-web-workspace.png)

<p>
  <img src="docs/screenshot-browser-agent.png" width="100%" />
</p>

---

## Project status

- **CLI** is the recommended and most stable entry point.
- **Web Workspace** is under active development — the frontend UX and flows are not finalized; expect rough edges.
- Rapidly iterating: data structures and interfaces may have breaking changes between commits.

---

## Quick start

### Requirements

- Python **3.11+**
- Node.js **18+** (web development only)

### Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r web/backend/requirements.txt
```

### Initialize

```bash
python -m aura init .
```

Edit `.aura/config/models.json` with your model profile (`base_url`, `model`, `api_key`, etc.).

### Run

```bash
python -m aura chat
```

<details>
<summary>Web Workspace (in development)</summary>

```bash
# Install frontend deps first
cd web/frontend && npm install

# Start both backend and frontend
./web-up.sh
```

Not guaranteed to be stable — for development and exploration only.

</details>

---

## Capabilities

### Working in v0

- **Workspace file organization**: scan, batch-rename, archive, generate an index and cleanup report, hash-based dedup
- **Document drafting ("vibe writing")**: turn scattered input into a structured first draft, with iterative edits and version diffs
- **Async progress with visibility**: explicit plan → phases → outputs; supports injecting materials or constraints mid-run

### In progress

- **Image/screenshot → table extraction**: multimodal-first, OCR fallback
- **Slide/report output** (basic formatting)
- **Read-only web research**: comparison matrices, evidence capture, provenance

### Out of scope for v0

- General-purpose desktop RPA (arbitrary GUI automation)
- One-shot generation of complex Excel workbooks (heavy formulas/pivots/macros)

---

## How it works

### WorkSpec: clarify first, then execute

AuraWork captures a task as a structured `WorkSpec` before doing any work:

- Goals and deliverables (expected outputs)
- Input materials (files / URLs / notes)
- Constraints (style, templates, deadlines, forbidden items)
- Resource scope (workspace root, file-type allowlist, domain allowlist)
- Risk/approval policy (what requires explicit approval)
- `intent_items`: clarified intent statements used for gating and audit

These fields gate tool use at runtime: out-of-scope paths, file types, or domains are denied or escalated.

### Planner / Worker split

| Role | Responsibility |
|---|---|
| **Planner** | Creates/updates the DAG; accepts or rejects proposals from Workers |
| **Workers** | Execute a single node; can propose changes, but cannot mutate the plan or self-escalate permissions |

Worker presets map to common office archetypes: `FileOps`, `Doc`, `Sheet`, `Browser` (read-only), `Verifier`.

Each node carries an execution contract (preset, allowed scope, expected outputs), so the same plan is both human-readable and directly schedulable/replayable.

### DAG parallel dispatch

The scheduler dispatches ready nodes within a concurrency cap. Dependency edges cover both semantic ordering and explicit serialization to avoid write conflicts.

Nodes can return proposals (additional steps, splits, validations). These go back to the Planner as incremental updates, keeping the planning/execution boundary clean.

### Self-healing loops

For low-level, high-frequency issues (format errors, formula references), Workers run a bounded **Action → Observe → Correct** loop internally before surfacing a failure upward.

### Structured intermediate format

Office/PDF files are converted to a structured intermediate representation (Markdown/JSON preserving heading levels, table boundaries, image positions), edited there, then written back to the target format for delivery.

---

## Approvals and control

### Progressive authorization

The workspace is the primary permission boundary. Low-risk actions (analysis, generating new files) proceed automatically. High-risk actions (overwrite / move / delete / run commands) require explicit approval.

### OperationPlan previews

Before any batch change, the system generates a readable preview: counts, operation-type breakdown, rule summary, and a details entry with diffs. The user decides whether to proceed or cancel.

### Approval surfacing and resume

Workers do not run interactive approvals internally. When a Worker needs a high-risk tool, it stops at the node boundary and surfaces a structured approval request (action summary, risk notes, diff/preview). The main loop pauses the run at a resumable checkpoint.

A single approval record can cover multiple pending tool calls to reduce repeated confirmations. After approval, the system executes the approved calls and injects the results back as a resume hint — no re-explaining context needed.

An optional "approval agent" can auto-classify `allow / deny / require_user` based on WorkSpec + preview; only `require_user` interrupts the user.

### Untrusted input governance

External content (web pages, PDFs, third-party files) is treated as data, not instructions:

- Used only for extraction, summarization, comparison, and evidence citation
- Action intent comes from `WorkSpec.intent_items`, not from external text
- High-side-effect actions must map to an intent item and cite relevant evidence

---

## Skills

Skills package an office deliverable into a reusable unit: clarification questions, DAG templates, tool constraints, acceptance checks, and output structure.

Built-in skills (`aura/builtin/skills/`):

| Skill | Description |
|---|---|
| `aura-docx` | Word document read/write and structured processing |
| `aura-pptx` | PowerPoint read/write |
| `aura-xlsx` | Excel/spreadsheet read/write |
| `aura-pdf` | PDF extraction and organization |
| `agent-browser` | Read-only web research and evidence capture |

---

## Roadmap

- [ ] Web Workspace: session management, event/timeline replay, artifact browser, approvals UI, DAG view
- [ ] More SkillPacks (archiving, documents, spreadsheets, research) and a more robust intermediate-format I/O layer
- [ ] Cleaner workspace bootstrap (start with files), tighter resource-scope constraints, visible run contracts
- [ ] Stronger execution isolation (container/VM) beyond the current logical-isolation baseline

---

## Third-party notices

Some built-in skills include Office Open XML schema resources. Notices are at:

```
aura/builtin/skills/*/ooxml/THIRD_PARTY_NOTICES.md
```

Preserve these notices when redistributing.

---

## Testing

```bash
pytest tests/
```

Tests cover DAG scheduling, parallel dispatch schema validation, approval manager threading, and event bus semantics. No API keys or external services required.

---

## Contributing

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for development setup, testing, and commit conventions.

---

## License

MIT. See [`LICENSE`](./LICENSE).
