"""Credential-free, strict offline interchange. No paths are opened by this module.

Artifacts enter as caller-supplied bytes and are sealed in SQLite. Package paths
are registry labels only, never filesystem authority. No raw Notion API payloads.
"""
import hashlib
import json
import re

MAX_BATCH_SOURCES = 50
MAX_PAGES = 100
MAX_DEPTH = 16
MAX_BLOCKS = 20000
MAX_TEXT = 64 * 1024
MAX_OUTPUT = 8 * 1024 * 1024
MAX_ATTACHMENTS = 50
MAX_ATTACHMENT = 10 * 1024 * 1024
MAX_ATTACHMENTS_BYTES = 100 * 1024 * 1024
MAX_BATCH_BYTES = 256 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_OBJECTS = 200000
MAX_JSON_DEPTH = 24
MAX_ISSUES = 1000
KINDS = {'png', 'jpg', 'jpeg', 'pdf', 'docx', 'xlsx'}
NODE_TYPES = {'page', 'paragraph', 'heading', 'list_item', 'quote', 'code', 'divider',
              'table', 'table_row', 'property', 'database', 'data_source', 'attachment',
              'hyperlink', 'bookmark', 'embed', 'relation', 'synced', 'reference'}
# External acquisition/source gaps only; never accept converter-derived issues.
GAPS = {'unknown_block', 'unsupported_block', 'reference_unresolved', 'relation_unresolved',
        'synced_unresolved', 'view_semantics_unresolved', 'pagination_incomplete',
        'permission_missing', 'resource_limit', 'external_attachment_rejected'}
INTERNAL_ISSUES = {'acquisition_partial', 'acquisition_failed', 'attachment_incomplete',
                   'issue_limit', 'empty_content'}


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def fields(value, required, optional=()):
    require(type(value) is dict and set(required) <= value.keys()
            and not value.keys() - set(required) - set(optional), 'Invalid package fields')


def identifier(value):
    require(type(value) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}', value)
            and '..' not in value, 'Invalid identifier')


def hash_value(value):
    require(type(value) is str and re.fullmatch('[0-9a-f]{64}', value), 'Invalid hash')


def text_value(value):
    require(type(value) is str and '\x00' not in value and len(value.encode()) <= MAX_TEXT,
            'Text resource limit')
    # Transport credentials must be stripped by the producer, including URLs embedded in text.
    require(not re.search(r'https?://[^\s]*\?', value, re.I), 'URL query data forbidden')
    require(not re.search(r'(?i)(?:https?://[^\s]*[?&#](?:[^\s]*)(?:signature|token|credential|x-amz-|x-goog-)|'
                          r'\b(?:authorization|cookie|set-cookie)\s*:|\bBearer\s+\S+|'
                          r'https?://[^\s/]+@)', value), 'Credential transport data forbidden')


def content_fingerprint(package):
    # Identity, importer attempts, package IDs and artifact storage labels are not content.
    return digest({k: package[k] for k in ('root', 'adapter_version', 'policy_version', 'api_version',
                                          'outcome', 'scope', 'nodes', 'gaps')} | {
        'artifacts': [{k: a[k] for k in ('id', 'kind', 'sha256', 'size')} for a in package['artifacts']]})


def reference_id(reference):
    return reference.replace('-', '')[-32:].lower()


def reference_fingerprint(reference):
    return digest({'notion_id': reference_id(reference)})


def load_package(raw):
    require(type(raw) is bytes and len(raw) <= MAX_JSON_BYTES, 'Package JSON size limit')
    # Preflight nesting before the JSON decoder allocates a deep recursive structure.
    depth = 0
    quoted = escaped = False
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif char == 34:
                quoted = False
        elif char == 34:
            quoted = True
        elif char in (91, 123):
            depth += 1
            require(depth <= MAX_JSON_DEPTH, 'Package nesting limit')
        elif char in (93, 125):
            depth -= 1
    def unique(pairs):
        result = {}
        for k, v in pairs:
            require(k not in result, 'Duplicate JSON key')
            result[k] = v
        return result
    try:
        package = json.loads(raw, object_pairs_hook=unique,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
    except (UnicodeError, RecursionError) as exc:
        raise ValueError('Invalid package JSON') from exc
    stack = [package]
    objects = 0
    while stack:
        value = stack.pop()
        objects += 1
        require(objects <= MAX_OBJECTS, 'Package object limit')
        if isinstance(value, dict):
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    try:
        validate_package(package)
    except (TypeError, KeyError, OverflowError) as exc:
        raise ValueError('Invalid package value') from exc
    return package


def validate_package(p):
    fields(p, ('schema', 'package_id', 'task_id', 'batch_id', 'source_id', 'revision', 'root',
               'adapter_version', 'policy_version', 'api_version', 'outcome', 'content_fingerprint',
               'artifacts', 'nodes', 'gaps', 'scope'))
    require(p['schema'] == 'notion-source-package-v1', 'Unsupported package schema')
    for key in ('package_id', 'task_id', 'batch_id', 'source_id', 'adapter_version', 'policy_version', 'api_version'):
        identifier(p[key])
    require(type(p['revision']) is int and p['revision'] >= 1, 'Invalid revision')
    fields(p['root'], ('type', 'id'))
    require(p['root']['type'] in {'page', 'database', 'data_source'}, 'Invalid root type')
    identifier(p['root']['id'])
    require(re.fullmatch(r'[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', p['root']['id']), 'Invalid root object ID')
    require(p['outcome'] in {'completed', 'partial', 'failed'}, 'Invalid outcome')
    fields(p['scope'], ('traversal_complete', 'pagination_complete', 'permissions_complete', 'view_semantics_resolved'))
    require(all(type(v) is bool for v in p['scope'].values()), 'Invalid scope assertions')
    require(type(p['gaps']) is list and len(p['gaps']) <= MAX_ISSUES, 'Issue limit')
    for gap in p['gaps']:
        fields(gap, ('code',), ('node_id',))
        require(gap['code'] in GAPS, 'Invalid gap')
        if 'node_id' in gap:
            identifier(gap['node_id'])
    if p['outcome'] == 'completed':
        require(all(p['scope'].values()) and not p['gaps'],
                'Completed package requires complete scope and no gaps')
    require(type(p['artifacts']) is list and len(p['artifacts']) <= MAX_ATTACHMENTS, 'Attachment count limit')
    paths, artifacts = set(), set()
    total = 0
    for a in p['artifacts']:
        fields(a, ('id', 'kind', 'sha256', 'size', 'path'))
        identifier(a['id'])
        hash_value(a['sha256'])
        require(a['kind'] in KINDS and type(a['size']) is int and 0 <= a['size'] <= MAX_ATTACHMENT,
                'Invalid artifact kind/size')
        require(type(a['path']) is str and re.fullmatch(r'artifacts/[A-Za-z0-9][A-Za-z0-9_.-]{0,119}', a['path'])
                and '..' not in a['path'], 'Unsafe artifact path')
        require(a['id'] not in artifacts and a['path'].casefold() not in paths, 'Duplicate artifact/path')
        artifacts.add(a['id'])
        paths.add(a['path'].casefold())
        total += a['size']
    require(total <= MAX_ATTACHMENTS_BYTES, 'Attachment total limit')
    require(type(p['nodes']) is list and len(p['nodes']) <= MAX_BLOCKS, 'Visited block limit')
    nodes, pages, text_bytes = {}, 0, 0
    for n in p['nodes']:
        fields(n, ('id', 'type', 'parent_id'), ('text', 'cells', 'artifact_id', 'expanded',
            'page_id', 'table_id', 'data_source_id', 'property_id'))
        identifier(n['id'])
        identifier(n['type'])
        require(n['id'] not in nodes, 'Duplicate node')
        if n['parent_id'] is not None:
            require(n['parent_id'] in nodes, 'Parent must precede child')
            require(nodes[n['parent_id']][0]['type'] not in {'hyperlink', 'bookmark', 'embed'}, 'Links cannot grant traversal')
        depth = 1 if n['parent_id'] is None else nodes[n['parent_id']][1] + 1
        require(depth <= MAX_DEPTH, 'Traversal depth limit')
        nodes[n['id']] = (n, depth)
        pages += n['type'] == 'page'
        for key in ('page_id', 'table_id', 'data_source_id', 'property_id', 'artifact_id'):
            if key in n:
                identifier(n[key])
        if 'expanded' in n:
            require(type(n['expanded']) is bool, 'Invalid expansion flag')
        if 'text' in n:
            text_value(n['text'])
            text_bytes += len(n['text'].encode())
        if 'cells' in n:
            require(n['type'] == 'table_row' and type(n['cells']) is list and len(n['cells']) <= 512, 'Invalid cells')
            for cell in n['cells']:
                text_value(cell)
                text_bytes += len(cell.encode())
        if 'artifact_id' in n:
            require(n['type'] == 'attachment' and n['artifact_id'] in artifacts, 'Unregistered artifact')
    require(pages <= MAX_PAGES and text_bytes <= MAX_OUTPUT, 'Source resource limit')
    if p['nodes']:
        require(p['nodes'][0]['id'] == p['root']['id'] and p['nodes'][0]['type'] == p['root']['type']
                and all(n['parent_id'] is not None for n in p['nodes'][1:]), 'Invalid root tree')
    else:
        require(p['outcome'] == 'failed', 'Missing root')
    hash_value(p['content_fingerprint'])
    require(content_fingerprint(p) == p['content_fingerprint'], 'Content fingerprint mismatch')


def convert(source, p, artifact_reader):
    from .content import parse_content
    result = parse_content(source, error='placeholder')
    result['issues'] = []
    base = dict(result['locator'], package_id=p['package_id'], source_revision=p['revision'])
    size = 0
    def gap(code, where=base):
        if len(result['issues']) < MAX_ISSUES - 1:
            result['issues'].append(dict(code=code, severity='warning', locator=dict(where)))
        elif len(result['issues']) == MAX_ISSUES - 1:
            result['issues'].append(dict(code='issue_limit', severity='warning', locator=base))
    def block(text, where):
        nonlocal size
        if not text.strip():
            return
        if len(text.encode()) > MAX_TEXT or size + len(text.encode()) > MAX_OUTPUT or len(result['blocks']) >= MAX_BLOCKS:
            gap('resource_limit', where)
            return
        size += len(text.encode())
        result['blocks'].append(dict(type='text', text=text, trust='untrusted_source_data', locator=dict(where)))
    locations = {}
    parents = {n['parent_id'] for n in p['nodes']}
    for n in p['nodes']:
        loc = dict(locations.get(n['parent_id'], base), notion_block_id=n['id'])
        for typ, key in (('page', 'notion_page_id'), ('table', 'notion_table_id'), ('data_source', 'notion_data_source_id'), ('property', 'notion_property_id')):
            if n['type'] == typ:
                loc[key] = n['id']
        for key in ('page_id', 'table_id', 'data_source_id', 'property_id'):
            if key in n:
                loc['notion_' + key] = n[key]
        locations[n['id']] = loc
        if n['type'] not in NODE_TYPES:
            gap('unknown_block', loc)
        if n['type'] in {'relation', 'synced', 'reference'} and (not n.get('expanded') or n['id'] not in parents):
            gap(n['type'] + '_unresolved', loc)
        if n['type'] in {'database', 'data_source'} and not n.get('expanded'):
            gap('view_semantics_unresolved', loc)
        if 'text' in n:
            block(n['text'], loc)
        for index, cell in enumerate(n.get('cells', [])):
            block(cell, dict(loc, column_index=index))
        if n['type'] == 'attachment' and 'artifact_id' not in n:
            gap('external_attachment_rejected', loc)
    # Parse every registered artifact once, even if a producer omitted its body reference.
    for a in p['artifacts']:
        loc = dict(base, attachment_id=a['id'], path=a['path'])
        attachment = dict(source, kind=a['kind'], locator=a['path'], sha256=a['sha256'], byte_length=a['size'])
        try:
            parsed = parse_content(attachment, artifact_reader(a))
        except (ValueError, OSError):
            parsed = parse_content(attachment, error='artifact_verification_failed')
        for b in parsed['blocks']:
            block(b['text'], dict(b['locator'], **loc))
        for issue in parsed['issues']:
            gap(issue['code'], dict(issue['locator'], **loc))
        if parsed['status'] != 'completed' and not parsed['issues']:
            gap('attachment_incomplete', loc)
    for flag, code in (('traversal_complete', 'resource_limit'), ('pagination_complete', 'pagination_incomplete'),
                       ('permissions_complete', 'permission_missing'), ('view_semantics_resolved', 'view_semantics_unresolved')):
        if not p['scope'][flag]:
            gap(code)
    for issue in p['gaps']:
        gap(issue['code'], locations.get(issue.get('node_id'), base))
    if p['outcome'] != 'completed':
        gap('acquisition_' + p['outcome'])
    if p['outcome'] == 'failed':
        result['blocks'] = []
    if not result['blocks']:
        gap('empty_content')
    result['status'] = 'failed' if not result['blocks'] else 'partial' if result['issues'] else 'completed'
    result['completeness'] = 'complete' if result['status'] == 'completed' else 'incomplete'
    return result
