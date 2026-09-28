"""Strict JSON protocol v1. Actor fields are supplied only by a trusted controller."""
import base64
import json
import re
from urllib.parse import urlsplit

from .sources import ALLOWED, MAX_SOURCE_BYTES
from .storage import safe_name

OPERATIONS = {
    'create_or_get_active', 'append_text', 'append_upload', 'append_notion_reference',
    'finish_collection', 'submit_user_message', 'get_status', 'get_summary',
    'confirm_generation', 'cancel', 'reset', 'get_artifact',
}
READS = {'get_status', 'get_summary', 'get_artifact'}


def validate_request(request):
    if not isinstance(request, dict):
        raise ValueError('Request must be an object')
    required = {'protocol_version', 'request_id', 'actor', 'operation', 'payload'}
    if not required <= request.keys() or request.keys() - required - {'task_id', 'expected_version'}:
        raise ValueError('Invalid request fields')
    if type(request['protocol_version']) is not int or request['protocol_version'] != 1:
        raise ValueError('Unsupported protocol_version')
    safe_name(request['request_id'])
    actor = request['actor']
    if not isinstance(actor, dict) or set(actor) != {'user_id', 'chat_id', 'chat_type'}:
        raise ValueError('Invalid actor fields')
    if any(not isinstance(v, str) or not v or len(v) > 128 for v in actor.values()):
        raise ValueError('Actor fields must be nonempty strings')
    op = request['operation']
    if not isinstance(op, str) or op not in OPERATIONS:
        raise ValueError('Unknown operation')
    if op != 'create_or_get_active':
        safe_name(request['task_id'])
    elif 'task_id' in request or 'expected_version' in request:
        raise ValueError('Create does not accept task_id/expected_version')
    if op not in READS | {'create_or_get_active'}:
        if type(request.get('expected_version')) is not int or request['expected_version'] < 1:
            raise ValueError('Write requires positive expected_version')
    p = request['payload']
    fields = {'append_text': {'text'}, 'append_upload': {'filename', 'data_base64'},
              'append_notion_reference': {'reference'}, 'submit_user_message': {'text'},
              'confirm_generation': {'text'}, 'get_artifact': {'artifact_id'}}.get(op, set())
    if not isinstance(p, dict) or set(p) != fields:
        raise ValueError('Invalid payload fields')
    if any(not isinstance(v, str) or not v for v in p.values()):
        raise ValueError('Payload values must be nonempty strings')
    if op in {'append_text', 'submit_user_message'} and len(p['text'].encode()) > MAX_SOURCE_BYTES:
        raise ValueError('Text exceeds 10 MiB')
    if op == 'append_upload':
        safe_name(p['filename'])
        if p['filename'].rsplit('.', 1)[-1].lower() not in ALLOWED:
            raise ValueError('Unsupported upload type')
        if len(p['data_base64']) > 4 * ((MAX_SOURCE_BYTES + 2) // 3):
            raise ValueError('Upload exceeds 10 MiB')
        data = base64.b64decode(p['data_base64'], validate=True)
        if len(data) > MAX_SOURCE_BYTES:
            raise ValueError('Upload exceeds 10 MiB')
    if op == 'get_artifact':
        safe_name(p['artifact_id'])
    if op == 'append_notion_reference':
        # Deliberately only canonical page IDs or canonical Notion URLs. No headers,
        # arbitrary metadata, userinfo, query parameters, fragments or credentials.
        ref = p['reference']
        compact = ref.replace('-', '')
        if re.fullmatch('[a-fA-F0-9]{32}', compact):
            return
        u = urlsplit(ref)
        if (u.scheme != 'https' or u.netloc not in {'notion.so', 'www.notion.so'}
                or u.query or u.fragment or len(ref) > 512
                or not re.fullmatch(r'/[A-Za-z0-9-]*[a-fA-F0-9]{32}', u.path)
                or re.search(r'authorization|token|cookie|secret|password|bearer|api.?key', ref, re.I)):
            raise ValueError('Use a canonical Notion page ID or URL without credential data')
    json.dumps(request, allow_nan=False)


def response(request_id, code, *, task=None, data=None, message=None):
    return {'protocol_version': 1, 'request_id': request_id, 'ok': code == 'ok',
            'code': code, 'task_id': task.id if task else None,
            'version': task.version if task else None, 'data': data or {},
            'error': {'message': message} if message else None}
