"""Synthetic Step 2C-A package import; no network, environment lookup, or real Notion."""
from pathlib import Path
import tempfile
from uuid import uuid4

from tg_testcase import Store
from tg_testcase.application import Application
from tg_testcase.acquisition import OfflineAcquisition
from tg_testcase.notion_package import content_fingerprint, encode
from tg_testcase.processor import Processor
from tg_testcase.storage import PROJECT


def main():
    scratch = PROJECT / '.test-runtime'
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as directory:
        store = Store(Path(directory) / 'data')
        app = Application(store, {'demo'})
        task = None

        def send(operation, payload=None):
            nonlocal task
            request = dict(protocol_version=1, request_id=uuid4().hex,
                           actor=dict(user_id='demo', chat_id='demo', chat_type='private'),
                           operation=operation, payload=payload or {})
            if task:
                request.update(task_id=task.id, expected_version=task.version)
            response = app.handle(request)
            if not response['ok']:
                raise ValueError(response['code'])
            task = store.get(response['task_id'])
            return response['data']

        send('create_or_get_active')
        root_id = 'a' * 32  # Synthetic identifier, never resolved.
        source = send('append_notion_reference', {'reference': root_id})
        send('finish_collection')
        processor = Processor(store)
        assert processor.claim(source['batch_id'], 'offline-worker') is None
        package = dict(schema='notion-source-package-v1', package_id=uuid4().hex,
            task_id=task.id, batch_id=source['batch_id'], source_id=source['source_id'], revision=1,
            root=dict(type='page', id=root_id), adapter_version='offline-example-1',
            policy_version='scope-1', api_version='not-applicable', outcome='completed',
            scope=dict(traversal_complete=True, pagination_complete=True,
                       permissions_complete=True, view_semantics_resolved=True),
            nodes=[dict(id=root_id, type='page', parent_id=None, text='Synthetic page title'),
                   dict(id='body', type='paragraph', parent_id=root_id, text='Synthetic source body')],
            artifacts=[], gaps=[])
        package['content_fingerprint'] = content_fingerprint(package)
        importer = OfflineAcquisition(store)
        claim = importer.claim(source['batch_id'], source['source_id'], 'offline-controller')
        importer.transition(claim, 'fetching')  # Logical state only; no fetch implementation.
        importer.seal(claim, encode(package).encode(), {})
        result = processor.process(source['batch_id'], 'offline-worker')
        print(encode(dict(status=result['status'], batch_input_fingerprint=result['batch_input_fingerprint'])))


if __name__ == '__main__':
    main()
