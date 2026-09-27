"""Review invariants and explicit traceability validation."""
import re
import unicodedata


def normalized(text):
    return re.sub(r"\W+", "", unicodedata.normalize("NFKC", text).casefold())


def indexed(items):
    result = {item.id: item for item in items}
    if len(result) != len(items) or any(not k.strip() for k in result):
        raise ValueError("IDs must be nonempty and unique")
    return result


def require_refs(refs, index):
    if not refs or len(refs) != len(set(refs)) or any(r not in index for r in refs):
        raise ValueError("Missing, duplicate or dangling traceability references")


def validate(task):
    sources = indexed(task.sources)
    requirements = indexed(task.requirements)
    questions = indexed(task.questions)
    rules = indexed(task.rules)
    paths = indexed(task.paths)
    indexed(task.cases)
    for req in task.requirements:
        if not req.text.strip():
            raise ValueError("Empty requirement")
        require_refs(req.source_ids, sources)
    keys, texts, rounds = set(), set(), {}
    for q in task.questions:
        key, text = normalized(q.key), normalized(q.text)
        if not key or not text or key in keys or text in texts:
            raise ValueError("Repeated question")
        keys.add(key)
        texts.add(text)
        require_refs(q.requirement_ids, requirements)
        if q.priority not in {"P0", "P1"} or q.round < 1:
            raise ValueError("Invalid question priority/round")
        if q.conflict:
            refs = {s for r in q.requirement_ids for s in requirements[r].source_ids}
            if q.priority != "P0" or len(refs) < 2:
                raise ValueError("Critical multi-source conflict must be P0 with two sources")
        if q.skipped and q.priority != "P1":
            raise ValueError("P0 cannot be skipped")
        if q.priority == "P1":
            rounds[q.round] = rounds.get(q.round, 0) + 1
            if rounds[q.round] > 3:
                raise ValueError("At most 3 P1 questions per round")
    answered = set()
    for answer in task.answers:
        if answer.question_id not in questions or answer.question_id in answered:
            raise ValueError("Unknown or already answered question")
        if answer.user_id != task.user_id or not answer.text.strip():
            raise ValueError("Answer must belong to owner and contain text")
        if questions[answer.question_id].skipped:
            raise ValueError("Skipped question cannot have an answer")
        answered.add(answer.question_id)
    for rule in task.rules:
        require_refs(rule.requirement_ids, requirements)
        if not rule.text.strip():
            raise ValueError("Empty final rule")
        if rule.question_id is not None:
            if rule.question_id not in answered:
                raise ValueError("Final rule requires a user answer")
            if not set(rule.requirement_ids).issubset(questions[rule.question_id].requirement_ids):
                raise ValueError("Final rule does not match question requirements")
    for path in task.paths:
        if not path.ai:
            require_refs(path.rule_ids, rules)
        elif path.rule_ids:
            require_refs(path.rule_ids, rules)
        if not path.text.strip() or not path.checks or any(not c.strip() for c in path.checks):
            raise ValueError("Path needs explicit acceptance checks")
        if len(set(path.checks)) != len(path.checks):
            raise ValueError("Duplicate acceptance checks")
    for case in task.cases:
        if not all(s.strip() for s in (case.id, case.module, case.description)):
            raise ValueError("Case needs ID, module and description")
        if not case.steps or not case.expected or any(not s.strip() for s in case.steps + case.expected):
            raise ValueError("Case needs real steps and expected outcomes")
        for e in case.evidence:
            if e.path_id not in paths or e.check not in paths[e.path_id].checks:
                raise ValueError("Unknown path/check in testcase evidence")
            if e.step not in case.steps or e.expected not in case.expected:
                raise ValueError("Evidence must reference actual steps and expected outcomes")


def unresolved_p0(task):
    resolved = {r.question_id for r in task.rules if r.question_id}
    return [q.id for q in task.questions if q.priority == "P0" and q.id not in resolved]
