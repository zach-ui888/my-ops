from dataclasses import asdict, dataclass, field
from enum import Enum
import json


class State(str, Enum):
    COLLECTING = "collecting"
    REVIEW = "review"
    READY = "ready"
    GENERATING = "generating"
    GENERATED = "generated"
    CANCELLED = "cancelled"


TRANSITIONS = {
    State.COLLECTING: {State.REVIEW, State.CANCELLED},
    State.REVIEW: {State.READY, State.CANCELLED},
    State.READY: {State.REVIEW, State.GENERATING, State.CANCELLED},
    State.GENERATING: {State.GENERATED, State.READY, State.CANCELLED},
    State.GENERATED: {State.REVIEW, State.CANCELLED},
    State.CANCELLED: set(),
}


@dataclass
class Source:
    id: str
    kind: str
    locator: str
    content: str = ""
    references: list[str] = field(default_factory=list)
    status: str = "pending"
    complete: bool = False
    critical: bool = True
    failure: str = ""
    origin: str = "legacy_unknown"
    input_sha256: str | None = None
    byte_length: int | None = None
    revision: int = 1
    # Optional component-level completeness, e.g. Notion attachments/subpages.
    components: dict[str, bool] = field(default_factory=dict)

    @property
    def readable(self):
        return self.status == "parsed" and self.complete and all(self.components.values())


@dataclass
class Requirement:
    id: str
    text: str
    source_ids: list[str]


@dataclass
class Question:
    id: str
    key: str  # Stable semantic key supplied by the reviewer; not an LLM deduplicator.
    text: str
    priority: str
    requirement_ids: list[str]
    round: int = 1
    conflict: bool = False
    skipped: bool = False


@dataclass
class Answer:
    question_id: str
    user_id: str
    text: str


@dataclass
class FinalRule:
    id: str
    text: str
    requirement_ids: list[str]
    question_id: str | None = None


@dataclass
class CriticalPath:
    id: str
    text: str
    rule_ids: list[str]
    checks: list[str]  # Explicit acceptance assertions, not just path IDs.
    ai: bool = False


@dataclass
class Evidence:
    path_id: str
    check: str
    step: str
    expected: str


@dataclass
class TestCase:
    id: str
    module: str
    description: str
    preconditions: str
    steps: list[str]
    expected: list[str]
    evidence: list[Evidence] = field(default_factory=list)
    ai: bool = False


@dataclass
class Task:
    id: str
    user_id: str
    state: str = State.COLLECTING
    version: int = 1
    sources: list[Source] = field(default_factory=list)
    requirements: list[Requirement] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    answers: list[Answer] = field(default_factory=list)
    rules: list[FinalRule] = field(default_factory=list)
    paths: list[CriticalPath] = field(default_factory=list)
    cases: list[TestCase] = field(default_factory=list)
    confirmation: dict | None = None
    outputs: list[dict] = field(default_factory=list)
    recovery_note: str = ""
    pending_messages: list[dict] = field(default_factory=list)

    processing_barrier: list[dict] = field(default_factory=list)

    def transition(self, target):
        if target not in TRANSITIONS[self.state]:
            raise ValueError(f"Invalid transition: {self.state} -> {target}")
        self.state = target

    def dumps(self):
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def loads(cls, payload):
        d = json.loads(payload)
        for key, model in [("sources", Source), ("requirements", Requirement),
                           ("questions", Question), ("answers", Answer),
                           ("rules", FinalRule), ("paths", CriticalPath)]:
            d[key] = [model(**item) for item in d[key]]
        for item in d["cases"]:
            item["evidence"] = [Evidence(**e) for e in item["evidence"]]
        d["cases"] = [TestCase(**item) for item in d["cases"]]
        return cls(**d)
