from __future__ import annotations

from dataclasses import dataclass

from .models.task_result import OperationPlan


@dataclass(frozen=True, slots=True)
class ApprovalPolicy:
    """
    Simple approval policy for OperationPlan previews.

    This policy is intentionally conservative:
    - Any deletes require approval.
    - Large overwrites require approval.
    """

    overwrite_threshold: int = 5

    def should_require_approval(self, plan: OperationPlan) -> bool:
        breakdown = plan.breakdown
        if breakdown is None:
            return False

        if (breakdown.delete or 0) > 0:
            return True
        if (breakdown.overwrite or 0) > self.overwrite_threshold:
            return True
        return False

    def calculate_risk_level(self, plan: OperationPlan) -> int:
        """
        Return a coarse risk score: 0 (none) .. 4 (very high).
        """

        breakdown = plan.breakdown
        if breakdown is None:
            return 0

        deletes = breakdown.delete or 0
        overwrites = breakdown.overwrite or 0

        if deletes:
            return 4 if deletes > 10 else 3
        if overwrites > 20:
            return 3
        if overwrites > self.overwrite_threshold:
            return 2
        if (breakdown.create or 0) > 0 or overwrites > 0 or (breakdown.rename or 0) > 0 or (breakdown.move or 0) > 0:
            return 1
        return 0

