import json
from pathlib import Path
from uuid import uuid4

from .coverage import check_coverage, source_gaps
from .excel import render, validate_export
from .gates import gate
from .models import Answer, FinalRule, Source, State
from .review import unresolved_p0, validate
from .sources import ALLOWED, MAX_SOURCE_BYTES, TextParser
from .storage import PROJECT, atomic_write, inside_project, safe_name


class Engine:
    """Phase 1 offline compatibility API. Controllers must use Application.handle.

    Store and domain objects are trusted internal APIs, not an RPC interface.
    """
    def __init__(self, store, whitelist, template=None):
        self.store = store
        self.whitelist = {str(u) for u in whitelist}
        self.template = inside_project(template or PROJECT / "templates/testcase_template.xlsx")

    def _authorize(self, user_id, chat_type):
        if str(user_id) not in self.whitelist or chat_type != "private":
            raise PermissionError("Whitelisted private-chat users only")

    def create(self, user_id, chat_type="private"):
        self._authorize(user_id, chat_type)
        return self.store.create(user_id)

    def get(self, task_id, user_id, chat_type="private"):
        self._authorize(user_id, chat_type)
        task = self.store.get(task_id)
        if task.user_id != str(user_id):
            raise PermissionError("Task owner mismatch")
        return task

    def _change(self, task_id, user_id, version, action, chat_type):
        self._authorize(user_id, chat_type)
        return self.store.change(task_id, user_id, version, action)

    @staticmethod
    def _edit(task):
        if task.state in {State.CANCELLED, State.GENERATING}:
            raise ValueError("Task cannot be edited in this state")
        if task.state in {State.READY, State.GENERATED}:
            task.transition(State.REVIEW)
        task.confirmation = None

    def add_source(self, task_id, user_id, version, filename, data, critical=True, chat_type="private"):
        # Compatibility path: offline callers retain immediate TextParser behavior.
        safe_name(filename)
        kind = Path(filename).suffix.lower().lstrip(".")
        if kind not in ALLOWED or not isinstance(data, bytes) or len(data) > MAX_SOURCE_BYTES:
            raise ValueError("Unsupported file type or upload exceeds 10 MiB")
        def action(task):
            self._edit(task)
            sid = uuid4().hex
            stored = f"{sid}-{filename}"
            source = TextParser().parse(Source(sid, kind, f"source/{stored}", critical=critical), data)
            atomic_write(self.store.directory(task.id) / "source" / stored, data)
            task.sources.append(source)
        return self._change(task_id, user_id, version, action, chat_type)

    def add_reference(self, task_id, user_id, version, kind, locator, critical=True, chat_type="private"):
        if kind != "notion" or not locator.strip():
            raise ValueError("Only a nonempty Notion locator is supported here")
        def action(task):
            self._edit(task)
            task.sources.append(Source(uuid4().hex, kind, locator, status="unsupported", critical=critical,
                                       failure="Notion parser is not implemented; no network request made"))
        return self._change(task_id, user_id, version, action, chat_type)

    def start_review(self, task_id, user_id, version, chat_type="private"):
        return self._change(task_id, user_id, version, lambda t: t.transition(State.REVIEW), chat_type)

    def design(self, task_id, user_id, version, requirements, rules, paths, cases, chat_type="private"):
        """Replace current design; previously confirmed rules may not silently change."""
        def action(task):
            self._edit(task)
            if task.state != State.REVIEW:
                raise ValueError("Start review first")
            incoming = {r.id: r for r in rules}
            for rule in task.rules:
                if rule.question_id and incoming.get(rule.id) != rule:
                    raise ValueError("Confirmed rule cannot be removed/changed; reset review explicitly")
            task.requirements, task.rules, task.paths, task.cases = requirements, rules, paths, cases
            validate(task)
        return self._change(task_id, user_id, version, action, chat_type)

    def ask(self, task_id, user_id, version, questions, chat_type="private"):
        def action(task):
            self._edit(task)
            if task.state != State.REVIEW:
                raise ValueError("Start review first")
            current_round = max((q.round for q in task.questions), default=1)
            if any(q.round < current_round or q.round > current_round + 1 for q in questions):
                raise ValueError("Review rounds must be consecutive")
            if any(q.round > current_round for q in questions) and not unresolved_p0(task):
                raise ValueError("Further review round requires unresolved P0")
            task.questions.extend(questions)
            validate(task)
        return self._change(task_id, user_id, version, action, chat_type)

    def answer(self, task_id, user_id, version, question_id, text, rule_id, final_text, chat_type="private"):
        def action(task):
            self._edit(task)
            q = next((q for q in task.questions if q.id == question_id), None)
            if not q:
                raise ValueError("Unknown question")
            task.answers.append(Answer(q.id, str(user_id), text))
            task.rules.append(FinalRule(rule_id, final_text, list(q.requirement_ids), q.id))
            validate(task)
        return self._change(task_id, user_id, version, action, chat_type)

    def skip(self, task_id, user_id, version, question_id, chat_type="private"):
        def action(task):
            self._edit(task)
            q = next((q for q in task.questions if q.id == question_id), None)
            if q is None:
                raise ValueError("Unknown question")
            q.skipped = True
            validate(task)
        return self._change(task_id, user_id, version, action, chat_type)

    def ready(self, task_id, user_id, version, chat_type="private"):
        def action(task):
            validate(task)
            task.transition(State.READY)
            task.confirmation = None
        return self._change(task_id, user_id, version, action, chat_type)

    def summary(self, task_id, user_id, chat_type="private"):
        task = self.get(task_id, user_id, chat_type)
        report = check_coverage(task).to_dict()
        report.update(version=task.version, state=task.state, unresolved_p0=unresolved_p0(task),
                      incomplete_sources=source_gaps(task),
                      p1_pending=[q.id for q in task.questions if q.priority == "P1" and not q.skipped
                                  and q.id not in {a.question_id for a in task.answers}])
        return report

    def confirm(self, task_id, user_id, version, text, chat_type="private"):
        def action(task):
            if task.state != State.READY or text not in {"生成", "带缺口生成草稿"}:
                raise ValueError("Reply exactly 生成 or 带缺口生成草稿 to a ready task")
            task.confirmation = {"mode": "formal" if text == "生成" else "draft",
                                 "version": task.version + 1, "user_id": task.user_id}
        return self._change(task_id, user_id, version, action, chat_type)

    def generate(self, task_id, user_id, version, mode="formal", chat_type="private"):
        def begin(task):
            gate(task, mode)
            task.transition(State.GENERATING)
        task = self._change(task_id, user_id, version, begin, chat_type)
        def finish(current):
            gate(current, mode)
            blob = render(self.template, current.cases, draft=mode == "draft")
            report = check_coverage(current, draft=mode == "draft").to_dict()
            name = f"{mode}-v{current.version + 1}.xlsx"
            output = inside_project(self.store.directory(current.id) / "output" / name)
            atomic_write(output, blob)
            regression = validate_export(self.template.read_bytes(), output.read_bytes(), current.cases, mode == "draft")
            atomic_write(output.with_suffix(".coverage.json"), json.dumps(report, ensure_ascii=False, indent=2).encode())
            current.outputs.append({"file": f"output/{name}", "version": current.version + 1,
                                    "mode": mode, "coverage": report, "regression": regression})
            current.transition(State.GENERATED)
            current.confirmation = None
        # On interruption/error GENERATING is durable. Startup recovery requires
        # reconfirmation; unreferenced files are retained, never auto-published.
        return self._change(task_id, user_id, task.version, finish, chat_type)

    def cancel(self, task_id, user_id, version, chat_type="private"):
        def action(task):
            task.transition(State.CANCELLED)
            task.confirmation = None
        return self._change(task_id, user_id, version, action, chat_type)

    def reset(self, task_id, user_id, version, chat_type="private"):
        def action(task):
            self._edit(task)
            if task.state == State.COLLECTING:
                task.transition(State.REVIEW)
            task.requirements, task.questions, task.answers = [], [], []
            task.rules, task.paths, task.cases = [], [], []
            # Old reviews and generated files remain as versioned history.
        return self._change(task_id, user_id, version, action, chat_type)
