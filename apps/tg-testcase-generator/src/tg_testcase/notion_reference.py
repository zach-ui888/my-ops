"""Offline full-match parser for the frozen §6 grammar. Never resolves a URL."""
import re
from .internal_errors import fail

_ID = r'[0-9A-Fa-f]{32}'
_UUID = r'[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}'
_ROOT = re.compile('(?:' + _ID + '|' + _UUID + ')')


def parse_notion_reference(text):
    if (type(text) is not str or not 1 <= len(text) <= 512 or
            any(ord(c) <= 32 or ord(c) >= 127 or c in '%\\?#' for c in text)):
        fail('REFERENCE_INVALID')
    if _ROOT.fullmatch(text):
        return text.replace('-', '').lower()
    match = re.fullmatch(r'https://(?:www\.)?notion\.so/([^/]{1,117})', text)
    if not match:
        fail('REFERENCE_INVALID')
    segment = match[1]
    if _ROOT.fullmatch(segment):
        return segment.replace('-', '').lower()
    for pattern in (_UUID, _ID):
        match = re.fullmatch(r'([A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)-(' + pattern + ')', segment)
        if match and len(match[1]) <= 80:
            return match[2].replace('-', '').lower()
    fail('REFERENCE_INVALID')
