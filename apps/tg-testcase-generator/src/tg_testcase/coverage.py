from dataclasses import asdict, dataclass
from .review import unresolved_p0, validate


@dataclass
class Coverage:
    covered: list[str]
    uncovered: list[str]
    missing_checks: dict[str, list[str]]
    ai_points: list[str]
    known_path_percent: float | None
    overall_complete: bool
    gaps: list[str]
    summary: dict

    def to_dict(self):
        return asdict(self)


def source_gaps(task):
    return [s.id for s in task.sources if not s.readable]


def check_coverage(task, draft=False):
    validate(task)
    sources = {s.id: s for s in task.sources}
    reqs = {r.id: r for r in task.requirements}
    rules = {r.id: r for r in task.rules}
    covered, uncovered, missing = [], [], {}
    for path in (p for p in task.paths if not p.ai):
        # A case ID alone is insufficient. Each acceptance check must have an
        # explicit link to a real step and expected result in a non-AI case.
        checks = {e.check for c in task.cases if not c.ai for e in c.evidence if e.path_id == path.id}
        absent = [c for c in path.checks if c not in checks]
        provenance_ok = all(sources[s].readable for rid in path.rule_ids
                            for req in rules[rid].requirement_ids for s in reqs[req].source_ids)
        if not provenance_ok:
            absent.append("来源读取不完整")
        if absent:
            uncovered.append(path.id)
            missing[path.id] = absent
        else:
            covered.append(path.id)
    gaps = [f"来源不完整:{s}" for s in source_gaps(task)]
    gaps += [f"未解决P0:{q}" for q in unresolved_p0(task)]
    business_requirements = {r for p in task.paths if not p.ai for rule in p.rule_ids
                             for r in rules[rule].requirement_ids}
    gaps += [f"需求未建立业务路径:{r.id}" for r in task.requirements if r.id not in business_requirements]
    business_rules = {r for p in task.paths if not p.ai for r in p.rule_ids}
    gaps += [f"最终规则未建立业务路径:{r.id}" for r in task.rules if r.id not in business_rules]
    if not task.sources:
        gaps.append("没有资料")
    if draft:
        gaps.append("带缺口草稿：资料/规则可能存在缺口，禁止宣称100%完整覆盖")
    total = len(covered) + len(uncovered)
    # Suppress 100%-like numeric claims whenever completeness is unestablished.
    percent = round(100 * len(covered) / total, 2) if total and not gaps else None
    resolved = {r.question_id for r in task.rules}
    return Coverage(covered, uncovered, missing, [p.id for p in task.paths if p.ai], percent,
                    bool(total and not uncovered and not gaps), gaps,
                    {"modules": len({c.module for c in task.cases}), "requirements": len(task.requirements),
                     "business_paths": total, "covered_paths": len(covered), "cases": len(task.cases),
                     "ai_cases": sum(c.ai for c in task.cases),
                     "p0_resolved": [q.id for q in task.questions if q.priority == "P0" and q.id in resolved],
                     "p1_resolved": [q.id for q in task.questions if q.priority == "P1" and q.id in resolved],
                     "p1_skipped": [q.id for q in task.questions if q.skipped]})
