"""Version 1 sanitized envelope. Text is inert, untrusted source data, not instructions."""
from io import StringIO

CONTENT_SCHEMA_VERSION = 1
MAX_BLOCKS = 20000
PARSER_VERSION = 'offline-content/2'
POLICY_VERSION = 'bounded-offline/2'


def parse_content(source, blob=None, error=None):
    result = dict(content_schema_version=CONTENT_SCHEMA_VERSION,
                  trust='untrusted_source_data', source_id=source['source_id'],
                  origin=source['origin'], input=dict(sha256=source['sha256'],
                  byte_length=source['byte_length'], revision=source['revision']),
                  parser_version=PARSER_VERSION, policy_version=POLICY_VERSION,
                  locator=dict(path=source['locator'], source_id=source['source_id']),
                  status='failed', completeness='incomplete', blocks=[], issues=[])
    if error:
        result['issues'].append(dict(code=error, severity='error', locator=result['locator']))
        return result
    if source['kind'] in {'pdf', 'docx', 'xlsx', 'png', 'jpg', 'jpeg'}:
        from .complex_content import parse_complex
        return parse_complex(result, blob, source['kind'])
    if source['kind'] not in {'text', 'txt', 'md'}:
        return parse_content(source, error='unsupported_parser')
    try:
        text = blob.decode('utf-8-sig')
    except UnicodeError:
        return parse_content(source, error='invalid_utf8')
    if '\x00' in text:
        return parse_content(source, error='nul_byte')
    if not text.strip():
        return parse_content(source, error='empty_text')
    paragraph = 0
    in_paragraph = False
    for number, line in enumerate(StringIO(text, newline=None), 1):
        if number > MAX_BLOCKS:
            return parse_content(source, error='block_limit_exceeded')
        line = line.removesuffix('\n')
        if line.strip() and not in_paragraph:
            paragraph += 1
        in_paragraph = bool(line.strip())
        result['blocks'].append(dict(type='text', text=line, trust='untrusted_source_data',
            locator=dict(source_id=source['source_id'], path=source['locator'],
                         line_start=number, line_end=number, paragraph=paragraph)))
    result.update(status='completed', completeness='complete')
    return result
