from typing import Protocol
from .models import Source


MAX_SOURCE_BYTES = 10 * 1024 * 1024
ALLOWED = {"txt", "md", "pdf", "docx", "xlsx", "png", "jpg", "jpeg"}


class Parser(Protocol):
    def parse(self, source: Source, data: bytes) -> Source: ...


class TextParser:
    def parse(self, source, data):
        if source.kind not in {"txt", "md", "text"}:
            source.status = "unsupported"
            source.failure = f"Parser not implemented: {source.kind}"
            return source
        try:
            source.content = data.decode("utf-8-sig")
            if not source.content.strip() or "\x00" in source.content:
                raise ValueError("Empty or binary text")
            source.status = "parsed"
            source.complete = True
        except (UnicodeError, ValueError):
            source.status = "failed"
            source.failure = "Requires nonempty UTF-8 text without NUL bytes"
        return source
