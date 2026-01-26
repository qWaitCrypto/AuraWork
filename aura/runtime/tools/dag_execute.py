from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from ..dag_plan_runner import DAGPlanRunner
from ..models import WorkSpec
from ..plan import PlanItem
from .runtime import ToolExecutionContext

if TYPE_CHECKING:
    from ..parallel_dispatch import NodeCompletionHandler
    from .subagent_runner import SubagentRunTool


@dataclass(slots=True)
class DAGExecuteNextTool:
    """
    Execute the next batch of ready nodes in the DAG plan.
    """

    dag_runner: DAGPlanRunner
    subagent_tool: SubagentRunTool

    name: ClassVar[str] = "dag__execute_next"
    description: ClassVar[str] = (
        "Execute the next batch of ready nodes in the DAG plan. "
        "Returns execution results for each node, aggregated proposals from subagents, "
        "and indicates if any node is blocked on approval. "
        "Call this repeatedly until 'finished' is true."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "max_nodes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": "Max nodes to dispatch (default: runner's max_parallel).",
            }
        },
        "additionalProperties": False,
    }

    async def execute_async(
        self,
        *,
        args: dict[str, Any],
        project_root,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        # Runtime imports to avoid circular dependency
        from ..parallel_dispatch import (
            NodeCompletionHandler,
            dispatch_nodes_parallel,
        )

        project_root = Path(project_root).expanduser().resolve()

        max_nodes = args.get("max_nodes")
        if max_nodes is not None:
            if isinstance(max_nodes, bool) or not isinstance(max_nodes, int):
                raise ValueError("Invalid 'max_nodes' (expected integer).")
            if max_nodes < 1 or max_nodes > 10:
                raise ValueError("Invalid 'max_nodes' (expected integer between 1 and 10).")

        original_max_parallel: int | None = None
        if isinstance(max_nodes, int):
            original_max_parallel = self.dag_runner.max_parallel
            self.dag_runner.max_parallel = max_nodes
            if self.dag_runner._scheduler is not None:
                self.dag_runner._scheduler.max_parallel = max_nodes
        try:
            nodes = self.dag_runner.get_dispatchable_nodes()
        finally:
            if original_max_parallel is not None:
                self.dag_runner.max_parallel = original_max_parallel
                if self.dag_runner._scheduler is not None:
                    self.dag_runner._scheduler.max_parallel = original_max_parallel

        if not nodes:
            finished = self.dag_runner.is_all_done()
            return {
                "ok": True,
                "dispatched": 0,
                "finished": finished,
                "node_results": {},
                "all_proposals": [],
                "blocked_node": None,
                "blocked_approval": None,
                "message": "All nodes completed" if finished else "No ready nodes to dispatch",
            }

        plan_state = self.dag_runner.plan_store.get()
        plan_by_id: dict[str, PlanItem] = {it.id: it for it in plan_state.plan}

        presets_by_id: dict[str, str] = {}
        work_specs_by_id: dict[str, dict[str, Any]] = {}
        invalid_nodes: list[dict[str, Any]] = []
        for node in nodes:
            meta = node.metadata if isinstance(getattr(node, "metadata", None), dict) else {}
            preset = meta.get("preset")
            ws = meta.get("work_spec")

            node_errors: list[str] = []
            if not isinstance(preset, str) or not preset.strip():
                node_errors.append("missing metadata.preset")
            if not isinstance(ws, dict):
                node_errors.append("missing metadata.work_spec")
            else:
                try:
                    # Validate base WorkSpec first.
                    WorkSpec.model_validate(ws)
                except Exception as e:
                    node_errors.append(f"invalid metadata.work_spec: {e}")

            if node_errors:
                invalid_nodes.append({"node_id": node.id, "errors": node_errors})
                continue

            presets_by_id[node.id] = preset.strip()
            # Inject dependency outputs into downstream inputs (strict DAG I/O passing).
            work_specs_by_id[node.id] = self._merge_work_spec_inputs_from_deps(
                base_work_spec=ws,
                node=node,
                plan_by_id=plan_by_id,
            )

        if invalid_nodes:
            return {
                "ok": False,
                "dispatched": 0,
                "finished": False,
                "node_results": {},
                "all_proposals": [],
                "blocked_node": None,
                "blocked_approval": None,
                "error_code": "missing_node_contract",
                "message": (
                    "DAG nodes are missing required execution metadata. "
                    "Update the plan via `update_plan` and include `metadata.preset` + `metadata.work_spec` for each node."
                ),
                "invalid_nodes": invalid_nodes,
            }

        def _preset_for_node(node) -> str:
            return presets_by_id[node.id]

        def _work_spec_for_node(node) -> dict[str, Any]:
            return work_specs_by_id[node.id]

        dispatch_results = await dispatch_nodes_parallel(
            nodes=nodes,
            subagent_tool=self.subagent_tool,
            preset_selector=_preset_for_node,
            work_spec_selector=_work_spec_for_node,
            project_root=project_root,
            context=context,
            goal=self.dag_runner.get_goal(),
            progress_summary=self.dag_runner.get_progress_summary(),
        )

        handler = NodeCompletionHandler()
        actions = handler.process_batch(dispatch_results)
        dispatch_by_id = {r.node_id: r for r in dispatch_results}

        node_results: dict[str, dict[str, Any]] = {}
        blocked_node: str | None = None
        blocked_approval: dict[str, Any] | None = None

        for action in actions:
            node_id = action.node_id
            preset_name = presets_by_id.get(node_id)
            raw_result = dispatch_by_id.get(node_id).result if node_id in dispatch_by_id else None
            node_result = self._build_node_result_record(
                node_id=node_id,
                preset=preset_name,
                work_spec=work_specs_by_id.get(node_id),
                dispatch_result=raw_result,
                action=action,
            )
            if action.action == "mark_completed":
                self.dag_runner.mark_completed(node_id, node_result=node_result)
                node_results[node_id] = {
                    "status": "completed",
                    "artifacts": list(action.artifacts),
                    "receipts_count": len(action.receipts),
                }
            elif action.action == "pause_for_approval":
                if blocked_node is None:
                    blocked_node = node_id
                    blocked_approval = action.approval_request
                node_results[node_id] = {
                    "status": "needs_approval",
                    "approval_request": action.approval_request,
                }
            elif action.action == "mark_failed":
                self.dag_runner.mark_failed(node_id, action.error or "Unknown failure", node_result=node_result)
                node_results[node_id] = {
                    "status": "failed",
                    "error": action.error,
                }
            elif action.action == "mark_error":
                self.dag_runner.mark_failed(node_id, action.error or "Unknown error", node_result=node_result)
                node_results[node_id] = {
                    "status": "error",
                    "error": action.error,
                }
            else:
                node_results[node_id] = {"status": action.action}

        all_proposals = handler.aggregate_proposals(actions)

        return {
            "ok": blocked_node is None,
            "dispatched": len(nodes),
            "finished": self.dag_runner.is_all_done() if blocked_node is None else False,
            "node_results": node_results,
            "all_proposals": all_proposals,
            "blocked_node": blocked_node,
            "blocked_approval": blocked_approval,
        }

    @staticmethod
    def _parse_report(result: dict[str, Any] | None) -> dict[str, Any]:
        if result is None:
            return {}
        report = result.get("report")
        if report is None:
            return {}
        if isinstance(report, dict):
            return report
        if isinstance(report, str):
            try:
                parsed = json.loads(report)
            except (json.JSONDecodeError, TypeError):
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    def _build_node_result_record(
        self,
        *,
        node_id: str,
        preset: str | None,
        work_spec: dict[str, Any] | None,
        dispatch_result: dict[str, Any] | None,
        action: Any,
    ) -> dict[str, Any]:
        # Keep this record small; it is persisted in PlanStore metadata and used as DAG dependency input.
        report = self._parse_report(dispatch_result)
        raw_report = dispatch_result.get("report") if isinstance(dispatch_result, dict) else None
        report_text_preview = ""
        if isinstance(raw_report, str):
            report_text_preview = raw_report.strip()
        elif isinstance(raw_report, dict):
            try:
                report_text_preview = json.dumps(raw_report, ensure_ascii=False)
            except Exception:
                report_text_preview = ""
        max_preview = 4000
        if len(report_text_preview) > max_preview:
            report_text_preview = report_text_preview[:max_preview] + "…"

        return {
            "node_id": node_id,
            "preset": preset,
            "status": getattr(action, "action", None),
            "report": report if report else None,
            "report_preview": report_text_preview or None,
            "artifacts": list(getattr(action, "artifacts", ())),
            "receipts_count": len(getattr(action, "receipts", ())),
            "expected_outputs": (work_spec or {}).get("expected_outputs") if isinstance(work_spec, dict) else None,
        }

    @staticmethod
    def _merge_work_spec_inputs_from_deps(
        *,
        base_work_spec: dict[str, Any],
        node: PlanItem,
        plan_by_id: dict[str, PlanItem],
    ) -> dict[str, Any]:
        deps = [d for d in (node.depends_on or []) if isinstance(d, str) and d.strip()]
        if not deps:
            return base_work_spec

        # Copy so we don't mutate the plan contract stored in PlanStore.
        merged: dict[str, Any] = dict(base_work_spec)
        existing_inputs = merged.get("inputs")
        merged_inputs: list[dict[str, Any]] = []
        if isinstance(existing_inputs, list):
            merged_inputs.extend([x for x in existing_inputs if isinstance(x, dict)])

        # Avoid duplicating dependency inputs if the plan already contains them.
        seen_paths = {str(i.get("path") or "").strip() for i in merged_inputs if isinstance(i, dict)}

        for dep_id in deps:
            dep = plan_by_id.get(dep_id)
            if dep is None:
                continue

            dep_path = f"dag://{dep_id}"
            if dep_path in seen_paths:
                continue
            seen_paths.add(dep_path)

            dep_result = None
            if isinstance(dep.metadata, dict):
                dep_result = dep.metadata.get("node_result")

            dep_desc = ""
            if isinstance(dep_result, dict):
                try:
                    dep_desc = json.dumps(dep_result, ensure_ascii=False)
                except Exception:
                    dep_desc = ""
            max_desc = 6000
            if dep_desc and len(dep_desc) > max_desc:
                dep_desc = dep_desc[:max_desc] + "…"

            merged_inputs.append(
                {
                    "type": "connector_object",
                    "path": dep_path,
                    "description": dep_desc or "Upstream node output (no structured record available).",
                }
            )

            # Also pass along file outputs (if any) as file inputs for convenience.
            dep_ws = dep.metadata.get("work_spec") if isinstance(dep.metadata, dict) else None
            if isinstance(dep_ws, dict):
                outs = dep_ws.get("expected_outputs")
                if isinstance(outs, list):
                    for out in outs:
                        if not isinstance(out, dict):
                            continue
                        p = out.get("path")
                        if not isinstance(p, str) or not p.strip():
                            continue
                        rel = p.strip().lstrip("./")
                        if not rel:
                            continue
                        if rel in seen_paths:
                            continue
                        seen_paths.add(rel)
                        merged_inputs.append(
                            {
                                "type": "file",
                                "path": rel,
                                "description": f"Output from dependency node {dep_id}",
                            }
                        )

        if merged_inputs:
            merged["inputs"] = merged_inputs
        return merged

    def execute(
        self,
        *,
        args: dict[str, Any],
        project_root,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, self.execute_async(args=args, project_root=project_root, context=context))
                return future.result()

        return asyncio.run(self.execute_async(args=args, project_root=project_root, context=context))
