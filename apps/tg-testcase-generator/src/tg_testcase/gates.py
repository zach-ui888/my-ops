from .models import State
from .review import unresolved_p0, validate


class GenerationBlocked(ValueError):
    def __init__(self, reasons):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


def gate(task, mode):
    validate(task)
    reasons = []
    if any(b["status"] in {"open", "pending", "claimed", "processing"} for b in task.processing_barrier):
        reasons.append("Collection processing pending")
    if task.pending_messages:
        reasons.append("User messages pending review processor")
    if mode not in {"formal", "draft"}:
        reasons.append("Unknown generation mode")
    if task.state not in {State.READY, State.GENERATING}:
        reasons.append("Task not ready")
    c = task.confirmation
    # Confirmation is bound to an immutable content version. Entering generating
    # consumes exactly one subsequent task version, without altering content.
    expected_version = task.version if task.state == State.READY else task.version - 1
    if not c or c != {"mode": mode, "version": expected_version, "user_id": task.user_id}:
        reasons.append("Explicit current-version generation confirmation required")
    if mode == "formal":
        if unresolved_p0(task):
            reasons.append("Unresolved P0")
        if not task.sources or any(s.critical and not s.readable for s in task.sources):
            reasons.append("Critical source incomplete")
    if not task.requirements or not task.paths or not task.cases:
        reasons.append("Requirements, paths and actual cases required")
    if reasons:
        raise GenerationBlocked(reasons)
