from __future__ import annotations

import subprocess
from collections import Counter
from pathlib import Path

from ..models.task_result import OperationBreakdown, OperationPlan, OperationPlanItem, OperationType


class OperationPlanBuilder:
    """Build an OperationPlan from a git worktree's staged changes."""

    def build_from_worktree(self, worktree_path: Path, node_id: str) -> OperationPlan:
        worktree_path = Path(worktree_path).expanduser().resolve()

        result = subprocess.run(
            ["git", "-C", str(worktree_path), "diff", "--cached", "--name-status"],
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            return OperationPlan(summary="No changes", total_ops=0, breakdown=OperationBreakdown(), items=[])

        text = result.stdout or ""
        if not text.strip():
            # Fallback for callers that didn't stage changes.
            unstaged = subprocess.run(
                ["git", "-C", str(worktree_path), "diff", "--name-status"],
                capture_output=True,
                text=True,
                check=False,
            )
            if unstaged.returncode == 0:
                text = unstaged.stdout or ""

        items, counts = self._parse_diff_output(text)
        if not items:
            return OperationPlan(summary="No changes", total_ops=0, breakdown=OperationBreakdown(), items=[])

        breakdown = OperationBreakdown(
            create=counts.get(OperationType.CREATE),
            overwrite=counts.get(OperationType.OVERWRITE),
            rename=counts.get(OperationType.RENAME),
            delete=counts.get(OperationType.DELETE),
        )
        return OperationPlan(
            summary=self._generate_summary(node_id=node_id, counts=counts),
            total_ops=len(items),
            breakdown=breakdown,
            items=items,
        )

    def _parse_diff_output(self, text: str) -> tuple[list[OperationPlanItem], dict[OperationType, int]]:
        items: list[OperationPlanItem] = []
        counts: Counter[OperationType] = Counter()

        for raw in (text or "").splitlines():
            line = raw.strip()
            if not line:
                continue

            parts = line.split("\t")
            if not parts:
                continue

            status = parts[0].strip()
            if not status:
                continue

            code = status[0].upper()
            if code == "A" and len(parts) >= 2:
                target = parts[1].strip()
                items.append(OperationPlanItem(op=OperationType.CREATE, target=target))
                counts[OperationType.CREATE] += 1
                continue

            if code == "M" and len(parts) >= 2:
                target = parts[1].strip()
                items.append(OperationPlanItem(op=OperationType.OVERWRITE, target=target))
                counts[OperationType.OVERWRITE] += 1
                continue

            if code == "D" and len(parts) >= 2:
                target = parts[1].strip()
                items.append(OperationPlanItem(op=OperationType.DELETE, target=target))
                counts[OperationType.DELETE] += 1
                continue

            if code == "R" and len(parts) >= 3:
                old = parts[1].strip()
                new = parts[2].strip()
                items.append(OperationPlanItem(op=OperationType.RENAME, target=new, from_=old))
                counts[OperationType.RENAME] += 1
                continue

            if code == "C" and len(parts) >= 3:
                old = parts[1].strip()
                new = parts[2].strip()
                items.append(OperationPlanItem(op=OperationType.CREATE, target=new, from_=old, reason="copied"))
                counts[OperationType.CREATE] += 1
                continue

            # Conservative fallback: treat unknown statuses as overwrite of the path (if present).
            if len(parts) >= 2:
                target = parts[1].strip()
                items.append(OperationPlanItem(op=OperationType.OVERWRITE, target=target, reason=f"git_status:{status}"))
                counts[OperationType.OVERWRITE] += 1

        return items, dict(counts)

    def _generate_summary(self, *, node_id: str, counts: dict[OperationType, int]) -> str:
        parts: list[str] = []
        for k in (OperationType.CREATE, OperationType.OVERWRITE, OperationType.RENAME, OperationType.DELETE):
            n = counts.get(k, 0)
            if n:
                parts.append(f"{k.value}={n}")
        if not parts:
            return "No changes"
        return f"[{node_id}] " + ", ".join(parts)
