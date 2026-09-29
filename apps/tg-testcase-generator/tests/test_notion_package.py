"""Step 2C-A acceptance: synthetic packages only, mandatory offline parsers."""
import copy
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from PIL import Image
import pypdf
import defusedxml

from tg_testcase import Store
from tg_testcase.application import Application
from tg_testcase.acquisition import OfflineAcquisition
from tg_testcase.processor import Processor, LeaseLost
from tg_testcase import notion_package as n
from tg_testcase.migrations import migrate
from tg_testcase.storage import PROJECT
from tg_testcase.coverage import check_coverage
from tg_testcase.gates import gate, GenerationBlocked
from test_complex_content import doc, book

ROOT = 'a' * 32


def package(**binding):
    return dict(schema='notion-source-package-v1', package_id=uuid4().hex,
        task_id='task', batch_id='batch', source_id='source', revision=1,
        root=dict(type='page', id=ROOT), adapter_version='offline-1', policy_version='scope-1',
        api_version='2025-09-03', outcome='completed', content_fingerprint='0'*64,
        scope=dict(traversal_complete=True, pagination_complete=True,
                   permissions_complete=True, view_semantics_resolved=True),
        artifacts=[], nodes=[dict(id=ROOT, type='page', parent_id=None, text='Root title')], gaps=[]) | binding


def raw(p):
    p['content_fingerprint'] = n.content_fingerprint(p)
    return n.encode(p).encode()


def artifact(p, blob, kind, aid='a'):
    p['artifacts'].append(dict(id=aid, kind=kind, sha256=hashlib.sha256(blob).hexdigest(),
                               size=len(blob), path='artifacts/'+aid+'.'+kind))
    p['nodes'].append(dict(id='node-'+aid, type='attachment', parent_id=ROOT, artifact_id=aid))
    return {aid: blob}


def parse(p, blobs=None):
    p = n.load_package(raw(p))
    source = dict(source_id='source', kind='notion', origin='notion_reference', locator=ROOT,
                  revision=1, sha256=p['content_fingerprint'], byte_length=len(raw(p)))
    result = n.convert(source, p, lambda a: (blobs or {})[a['id']])
    contract = json.loads((PROJECT/'schemas/sanitized-content-v1.schema.json').read_text())['properties']['issues']['items']['properties']['code']
    assert contract['type'] == 'string' and contract['minLength'] == 1
    for issue in result['issues']:
        assert isinstance(issue['code'], str) and len(issue['code']) >= contract['minLength']
        assert 'enum' not in contract or issue['code'] in contract['enum']
    return result


class PackageTests(unittest.TestCase):
    def test_completed_requires_consistent_scope_and_no_gaps(self):
        schema = json.loads((PROJECT/'schemas/notion-source-package-v1.schema.json').read_text())
        assertions = schema['allOf'][0]['then']['properties']
        self.assertEqual(assertions['gaps']['maxItems'], 0)
        for flag in package()['scope']:
            self.assertEqual(assertions['scope']['properties'][flag], {'const': True})
            p = package()
            p['scope'][flag] = False
            raw(p)
            with self.subTest(flag=flag):
                with self.assertRaisesRegex(ValueError, 'Completed package'):
                    n.validate_package(p)
                with self.assertRaisesRegex(ValueError, 'Completed package'):
                    n.load_package(raw(p))
        for code in n.GAPS:
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, 'Completed package'):
                n.load_package(raw(package(gaps=[dict(code=code)])))

    def test_internal_issues_cannot_be_supplied_as_package_gaps(self):
        schema = json.loads((PROJECT/'schemas/notion-source-package-v1.schema.json').read_text())
        allowed = set(schema['properties']['gaps']['items']['properties']['code']['enum'])
        self.assertEqual(allowed, n.GAPS)
        self.assertTrue(n.GAPS.isdisjoint(n.INTERNAL_ISSUES))
        for code in n.INTERNAL_ISSUES | {'visual_unresolved', 'input_verification_failed'}:
            for outcome in ('completed', 'partial', 'failed'):
                with self.subTest(code=code, outcome=outcome), self.assertRaisesRegex(ValueError, 'Invalid gap'):
                    n.load_package(raw(package(outcome=outcome, gaps=[dict(code=code)])))

    def test_attachment_incomplete_without_parser_issue(self):
        from tg_testcase.content import parse_content
        p = package()
        blobs = artifact(p, b'unused', 'pdf')
        def incomplete(source, blob=None, error=None):
            result = parse_content(source, error=error or 'temporary')
            if not error:
                result.update(status='partial', issues=[])
            return result
        with patch('tg_testcase.content.parse_content', side_effect=incomplete):
            r = parse(p, blobs)
        self.assertEqual(r['status'], 'partial')
        self.assertEqual([i['code'] for i in r['issues']], ['attachment_incomplete'])

    def test_empty_and_blank_content_cannot_complete(self):
        for text in (None, '', ' \n\t'):
            p = package()
            p['nodes'][0].pop('text')
            if text is not None:
                p['nodes'][0]['text'] = text
            r = parse(p)
            self.assertEqual(r['status'], 'failed')
            self.assertEqual(r['blocks'], [])
            self.assertIn('empty_content', [i['code'] for i in r['issues']])

    def test_issue_limit_at_boundary_preserves_failure_signal(self):
        for count in (999, 1000, 1001):
            p = package(outcome='failed')
            p['nodes'] += [dict(id='u'+str(i), type='unknown', parent_id=ROOT) for i in range(count)]
            r = parse(p)
            self.assertEqual(r['status'], 'failed')
            self.assertEqual(r['blocks'], [])
            self.assertEqual(len(r['issues']), n.MAX_ISSUES)
            self.assertEqual(r['issues'][-1]['code'], 'issue_limit')

    def test_required_parser_versions(self):
        self.assertEqual((pypdf.__version__, Image.__version__, defusedxml.__version__), ('6.1.1', '11.3.0', '0.7.1'))

    def test_schema_unknown_fields_every_level(self):
        for target in ('root', 'node', 'artifact', 'gap', 'scope', 'top'):
            p = package()
            artifact(p, b'x', 'pdf')
            p['outcome'] = 'partial'
            p['gaps'] = [dict(code='permission_missing')]
            obj = {'root':p['root'], 'node':p['nodes'][0], 'artifact':p['artifacts'][0],
                   'gap':p['gaps'][0], 'scope':p['scope'], 'top':p}[target]
            obj['trusted'] = True
            with self.subTest(target=target), self.assertRaises(ValueError):
                n.load_package(raw(p))

    def test_credentials_and_signed_urls(self):
        for text in ('Authorization: Bearer placeholder', 'Cookie: placeholder',
                     'https://x.invalid/file?X-Amz-Signature=placeholder',
                     'https://x.invalid/file?token=placeholder', 'https://user@x.invalid/file'):
            p = package()
            p['nodes'][0]['text'] = text
            with self.subTest(text=text), self.assertRaises(ValueError):
                n.load_package(raw(p))

    def test_path_boundary_and_normalized_duplicates(self):
        for path in ('/absolute', '../traversal', 'artifacts/../x', 'artifacts//x', 'artifacts/./x',
                     'artifacts\\x', 'C:/x', 'artifacts/%2e%2e', 'artifacts/x\x00'):
            p = package()
            artifact(p, b'x', 'pdf')
            p['artifacts'][0]['path'] = path
            with self.subTest(path=path), self.assertRaises(ValueError):
                n.load_package(raw(p))
        p = package()
        artifact(p, b'x', 'pdf', 'a')
        artifact(p, b'x', 'pdf', 'b')
        p['artifacts'][1]['path'] = 'artifacts/A.PDF'
        with self.assertRaises(ValueError):
            n.load_package(raw(p))

    def test_duplicate_artifact_and_node(self):
        p = package()
        artifact(p, b'x', 'pdf')
        p['artifacts'].append(copy.deepcopy(p['artifacts'][0]))
        with self.assertRaises(ValueError):
            n.load_package(raw(p))
        p = package()
        p['nodes'].append(copy.deepcopy(p['nodes'][0]))
        with self.assertRaises(ValueError):
            n.load_package(raw(p))

    def test_json_duplicate_nonfinite_nesting_and_object_limit(self):
        for blob in (b'{"x":0,"x":1}', b'{"x":NaN}', b'['*25+b'0'+b']'*25):
            with self.assertRaises(ValueError):
                n.load_package(blob)
        with patch.object(n, 'MAX_OBJECTS', 10), self.assertRaises(ValueError):
            n.load_package(raw(package()))
        with patch.object(n, 'MAX_JSON_BYTES', 10), self.assertRaises(ValueError):
            n.load_package(raw(package()))

    def test_bad_types_and_fingerprint(self):
        for key, val in (('revision', True), ('artifacts', {}), ('nodes', {}), ('outcome', 'trusted')):
            p = package(**{key:val})
            with self.assertRaises(ValueError):
                n.load_package(raw(p))
        p = package()
        raw(p)
        p['content_fingerprint'] = 'f'*64
        with self.assertRaises(ValueError):
            n.load_package(n.encode(p).encode())

    def test_root_tree_and_links_cannot_grant_traversal(self):
        p = package()
        p['root']['id'] = 'spoof-' + ROOT
        p['nodes'][0]['id'] = p['root']['id']
        with self.assertRaises(ValueError):
            n.load_package(raw(p))
        for parent in ('missing', 'link', None):
            p = package()
            p['nodes'] += [dict(id='link', type='bookmark', parent_id=ROOT, text='https://example.invalid'),
                           dict(id='child', type='page', parent_id=parent)]
            with self.assertRaises(ValueError):
                n.load_package(raw(p))

    def test_limits_at_boundary_and_over(self):
        p = package()
        p['nodes'][0]['text'] = 'x'*65536
        n.load_package(raw(p))
        p['nodes'][0]['text'] += 'x'
        with self.assertRaises(ValueError):
            n.load_package(raw(p))
        for constant, cap in [('MAX_PAGES',0), ('MAX_BLOCKS',0), ('MAX_DEPTH',0), ('MAX_OUTPUT',1)]:
            with self.subTest(constant=constant), patch.object(n, constant, cap), self.assertRaises(ValueError):
                n.load_package(raw(package()))
        p = package()
        artifact(p, b'xx', 'pdf')
        for constant in ('MAX_ATTACHMENTS','MAX_ATTACHMENT','MAX_ATTACHMENTS_BYTES'):
            with patch.object(n, constant, 0), self.assertRaises(ValueError):
                n.load_package(raw(p))
        p['gaps'] = [dict(code='permission_missing')]
        with patch.object(n, 'MAX_ISSUES',0), self.assertRaises(ValueError):
            n.load_package(raw(p))

    def test_actual_depth_and_page_limits(self):
        p = package()
        for i in range(1,16):
            p['nodes'].append(dict(id='n'+str(i), type='paragraph', parent_id=ROOT if i==1 else 'n'+str(i-1)))
        n.load_package(raw(p))
        p['nodes'].append(dict(id='overflow',type='paragraph',parent_id='n15'))
        with self.assertRaises(ValueError):
            n.load_package(raw(p))
        p = package()
        p['nodes'] += [dict(id='p'+str(i),type='page',parent_id=ROOT) for i in range(99)]
        n.load_package(raw(p))
        p['nodes'].append(dict(id='overflow',type='page',parent_id=ROOT))
        with self.assertRaises(ValueError):
            n.load_package(raw(p))

    def test_semantic_fingerprint_ignores_envelope_storage(self):
        p = package()
        artifact(p,b'x','pdf')
        q = copy.deepcopy(p)
        q.update(package_id='new', task_id='other', batch_id='other', revision=2)
        q['artifacts'][0]['path'] = 'artifacts/other.pdf'
        self.assertEqual(n.content_fingerprint(p), n.content_fingerprint(q))
        q['nodes'][0]['text'] = 'changed'
        self.assertNotEqual(n.content_fingerprint(p), n.content_fingerprint(q))
        self.assertEqual(n.reference_fingerprint(ROOT), n.reference_fingerprint('https://notion.so/title-'+ROOT))

    def test_structural_pages_tables_database_properties_locators(self):
        p = package()
        p['nodes'] += [dict(id='db',type='database',parent_id=ROOT,expanded=True),
            dict(id='ds',type='data_source',parent_id='db',expanded=True),
            dict(id='entry',type='page',parent_id='ds',text='Entry title'),
            dict(id='prop',type='property',parent_id='entry',text='Property value'),
            dict(id='table',type='table',parent_id='entry'),
            dict(id='row',type='table_row',parent_id='table',cells=['A','B']),
            dict(id='nested',type='paragraph',parent_id='prop',text='Nested body')]
        r = parse(p)
        self.assertEqual(r['status'],'completed')
        cell = next(b for b in r['blocks'] if b['text']=='B')
        for key,value in dict(package_id=p['package_id'],source_revision=1,notion_page_id='entry',
                              notion_block_id='row',notion_table_id='table',notion_data_source_id='ds',column_index=1).items():
            self.assertEqual(cell['locator'][key],value)
        self.assertTrue(all(b['trust']=='untrusted_source_data' for b in r['blocks']))

    def test_partial_scope_and_gaps_remain_partial(self):
        for code in n.GAPS:
            p = package(outcome='partial', gaps=[dict(code=code)])
            r = parse(p)
            self.assertEqual(r['status'],'partial')
            self.assertIn(code,[i['code'] for i in r['issues']])
        for flag in package()['scope']:
            p = package(outcome='partial')
            p['scope'][flag] = False
            self.assertEqual(parse(p)['status'],'partial')

    def test_unknown_unexpanded_external_attachment(self):
        for typ in ('future_type','reference','relation','synced','data_source','database','attachment'):
            p = package()
            p['nodes'].append(dict(id='n',type=typ,parent_id=ROOT))
            self.assertEqual(parse(p)['status'],'partial')

    def test_inert_hyperlink_bookmark_embed(self):
        p = package()
        for typ in ('hyperlink','bookmark','embed'):
            p['nodes'].append(dict(id=typ,type=typ,parent_id=ROOT,text='https://example.invalid/file'))
        with patch('socket.socket',side_effect=AssertionError('network')):
            self.assertEqual(parse(p)['status'],'completed')

    def test_attachment_parsers_and_partial_aggregation(self):
        for kind,blob in (('docx',doc()),('xlsx',book()),('pdf',b'invalid')):
            p = package()
            r = parse(p,artifact(p,blob,kind))
            self.assertEqual(r['status'],'partial' if kind=='pdf' else 'completed')
            self.assertTrue(any('attachment_id' in x['locator'] for x in r['blocks']+r['issues']))
        for kind,fmt in (('png','PNG'),('jpg','JPEG'),('jpeg','JPEG')):
            stream=BytesIO()
            Image.new('RGB',(2,2)).save(stream,format=fmt)
            p=package()
            r=parse(p,artifact(p,stream.getvalue(),kind))
            self.assertEqual(r['status'],'partial')
            self.assertIn('visual_unresolved',[i['code'] for i in r['issues']])

    def test_output_limits_include_attachments_and_cells(self):
        p=package()
        blobs=artifact(p,doc(),'docx')
        # Validate first; conversion must also budget the merged output.
        n.load_package(raw(p))
        with patch.object(n,'MAX_OUTPUT',12):
            source=dict(source_id='source',origin='notion_reference',locator=ROOT,revision=1,sha256=None,byte_length=None)
            r=n.convert(source,p,lambda a:blobs[a['id']])
            self.assertEqual(r['status'],'partial')
            self.assertIn('resource_limit',[i['code'] for i in r['issues']])
        p=package()
        p['nodes'].append(dict(id='row',type='table_row',parent_id=ROOT,cells=['a','b']))
        with patch.object(n,'MAX_BLOCKS',2):
            r=parse(p)
            self.assertEqual(r['status'],'partial')
            self.assertEqual(len(r['blocks']),2)

    def test_issue_count_bounded(self):
        p=package()
        p['nodes'] += [dict(id='n'+str(i),type='unknown',parent_id=ROOT) for i in range(10)]
        with patch.object(n,'MAX_ISSUES',3):
            r=parse(p)
            self.assertEqual(len(r['issues']),3)
            self.assertEqual(r['issues'][-1]['code'],'issue_limit')

    def test_failed_outcome_cannot_become_complete(self):
        r=parse(package(outcome='failed'))
        self.assertIn('acquisition_failed', [i['code'] for i in r['issues']])
        self.assertEqual(r['status'],'failed')
        self.assertEqual(r['blocks'],[])


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        base=PROJECT/'.test-runtime'
        base.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=base)
        self.store=Store(Path(self.tmp.name)/'data')
        self.app=Application(self.store,{'u'})
        self.task=None
        self.now=1000
        self.acq=OfflineAcquisition(self.store,clock=lambda:self.now)
        self.processor=Processor(self.store,clock=lambda:self.now)
        self.send('create_or_get_active')

    def tearDown(self):
        self.tmp.cleanup()

    def send(self,op,payload=None,ok=True):
        request=dict(protocol_version=1,request_id=uuid4().hex,actor=dict(user_id='u',chat_id='c',chat_type='private'),operation=op,payload=payload or {})
        if self.task:
            self.task=self.store.get(self.task.id)
            request.update(task_id=self.task.id,expected_version=self.task.version)
        result=self.app.handle(request)
        self.assertEqual(result['ok'],ok,result)
        if result['ok']:
            self.task=self.store.get(result['task_id'])
        return result

    def setup_source(self):
        r=self.send('append_notion_reference',{'reference':ROOT})['data']
        self.bid,self.sid=r['batch_id'],r['source_id']
        self.send('finish_collection')
        return package(task_id=self.task.id,batch_id=self.bid,source_id=self.sid)

    def seal(self,p,blobs=None):
        c=self.acq.claim(p['batch_id'],p['source_id'],'controller')
        self.acq.transition(c,'fetching')
        self.acq.seal(c,raw(p),blobs or {})
        return c

    def sql(self,statement,args=()):
        with self.store._locked() as conn:
            return conn.execute(statement,args).fetchall()

    def test_two_stage_and_offline_end_to_end(self):
        p=self.setup_source()
        self.assertIsNone(self.processor.claim(self.bid,'worker'))
        self.assertEqual(self.sql('SELECT acquisition_status,input_manifest_digest FROM collection_batches')[0],('pending',None))
        with patch('socket.socket',side_effect=AssertionError('network')), patch('subprocess.Popen',side_effect=AssertionError('process')), patch('os.getenv',side_effect=AssertionError('environment')):
            self.seal(p)
            result=self.processor.process(self.bid,'worker')
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['batch_input_fingerprint'],self.sql('SELECT digest FROM batch_input_manifests')[0][0])
        t=self.store.get(self.task.id)
        self.assertTrue(t.sources[0].readable)
        self.assertEqual([t.requirements,t.rules,t.questions,t.cases],[[],[],[],[]])
        self.assertIsNotNone(self.processor.result(self.bid)[1][0]['input']['sha256'])

    def test_mixed_order_and_fingerprints(self):
        a=self.send('append_text',{'text':'first'})['data']
        b=self.send('append_notion_reference',{'reference':ROOT})['data']
        self.send('finish_collection')
        p=package(task_id=self.task.id,batch_id=a['batch_id'],source_id=b['source_id'])
        self.assertIsNone(self.processor.claim(a['batch_id'],'w'))
        self.seal(p)
        manifest=json.loads(self.sql('SELECT payload FROM batch_input_manifests')[0][0])
        self.assertEqual([s['source_id'] for s in manifest['sources']],[a['source_id'],b['source_id']])
        self.assertEqual(manifest['sources'][0]['input_fingerprint'],hashlib.sha256(b'first').hexdigest())
        self.assertEqual(manifest['sources'][1]['input_fingerprint'],p['content_fingerprint'])

    def test_only_all_packages_seal_batch(self):
        a=self.send('append_notion_reference',{'reference':ROOT})['data']
        b=self.send('append_notion_reference',{'reference':ROOT})['data']
        self.send('finish_collection')
        for i,r in enumerate((a,b)):
            self.seal(package(task_id=self.task.id,batch_id=r['batch_id'],source_id=r['source_id']))
            self.assertEqual(len(self.sql('SELECT * FROM batch_input_manifests')),i)

    def test_binding_rejection_is_atomic(self):
        p=self.setup_source()
        c=self.acq.claim(self.bid,self.sid,'controller')
        self.acq.transition(c,'fetching')
        for key,val in (('task_id','other'),('batch_id','other'),('source_id','other'),('revision',2)):
            q=copy.deepcopy(p); q[key]=val
            with self.assertRaises(ValueError):
                self.acq.seal(c,raw(q),{})
        q=copy.deepcopy(p); q['root']['id']='b'*32; q['nodes'][0]['id']='b'*32
        with self.assertRaises(ValueError):
            self.acq.seal(c,raw(q),{})
        self.assertEqual(self.sql('SELECT * FROM source_packages'),[])
        self.assertEqual(self.sql('SELECT status FROM source_fetch_runs')[0][0],'fetching')

    def test_hash_size_registry_no_path_reads(self):
        p=self.setup_source()
        blobs=artifact(p,doc(),'docx')
        c=self.acq.claim(self.bid,self.sid,'controller'); self.acq.transition(c,'fetching')
        for bad in ({},{'a':b'wrong'},dict(blobs,extra=b'x')):
            with self.assertRaises(ValueError): self.acq.seal(c,raw(p),bad)
        with patch('tg_testcase.storage.bounded_read',side_effect=AssertionError('arbitrary read')):
            self.acq.seal(c,raw(p),blobs)
            self.assertEqual(self.processor.process(self.bid,'w')['status'],'completed')

    def test_sealed_immutable_and_attempt_replay(self):
        p=self.setup_source(); c=self.seal(p)
        with self.assertRaises(LeaseLost): self.acq.seal(c,raw(p),{})
        self.assertIsNone(self.acq.claim(self.bid,self.sid,'other'))
        for table in ('source_packages','batch_input_manifests'):
            with self.store._locked() as conn, conn:
                with self.assertRaises(sqlite3.IntegrityError): conn.execute('DELETE FROM '+table)
        p['nodes'][0]['text']='remote changed'
        self.processor.process(self.bid,'w')
        self.assertEqual(self.store.get(self.task.id).sources[0].content,'Root title')

    def test_retry_new_attempt_and_fencing(self):
        p=self.setup_source()
        c=self.acq.claim(self.bid,self.sid,'a',5)
        self.assertIsNone(self.acq.claim(self.bid,self.sid,'b'))
        self.acq.transition(c,'fetching'); self.acq.transition(c,'retry_wait',delay=1)
        self.assertIsNone(self.acq.claim(self.bid,self.sid,'b'))
        self.now+=2
        d=self.acq.claim(self.bid,self.sid,'b')
        self.assertEqual((d['attempt'],d['fencing_token']),(2,2))
        self.acq.transition(d,'fetching')
        with self.assertRaises(LeaseLost): self.acq.seal(c,raw(p),{})
        self.acq.seal(d,raw(p),{})

    def test_expiry_heartbeat_and_invalid_transition(self):
        self.setup_source()
        c=self.acq.claim(self.bid,self.sid,'a',1)
        with self.assertRaises(LeaseLost): self.acq.transition(c,'retry_wait')
        self.acq.heartbeat(c,3)
        self.now+=2
        self.assertIsNone(self.acq.claim(self.bid,self.sid,'b'))
        self.now+=2
        with self.assertRaises(LeaseLost): self.acq.heartbeat(c)
        self.assertIsNotNone(self.acq.claim(self.bid,self.sid,'b'))

    def test_cancel_abandons_unsealed_only(self):
        p=self.setup_source()
        c=self.acq.claim(self.bid,self.sid,'a'); self.acq.transition(c,'fetching')
        self.send('cancel')
        self.assertEqual(self.sql('SELECT status FROM source_fetch_runs')[0][0],'abandoned')
        with self.assertRaises(LeaseLost): self.acq.seal(c,raw(p),{})
        self.assertIsNone(self.acq.claim(self.bid,self.sid,'b'))

    def test_abandon_prevents_reclaim(self):
        self.setup_source(); c=self.acq.claim(self.bid,self.sid,'a')
        self.acq.transition(c,'abandoned')
        self.now+=100
        self.assertIsNone(self.acq.claim(self.bid,self.sid,'b'))

    def test_batch_budget_atomic_failure(self):
        p=self.setup_source(); c=self.acq.claim(self.bid,self.sid,'a'); self.acq.transition(c,'fetching')
        with patch('tg_testcase.acquisition.MAX_BATCH_BYTES',1), self.assertRaises(ValueError):
            self.acq.seal(c,raw(p),{})
        self.assertEqual(self.sql('SELECT * FROM source_packages'),[])
        self.assertEqual(self.sql('SELECT * FROM batch_input_manifests'),[])

    def test_artifact_insert_failure_full_rollback(self):
        p=self.setup_source(); blobs=artifact(p,doc(),'docx')
        c=self.acq.claim(self.bid,self.sid,'a'); self.acq.transition(c,'fetching')
        with self.store._locked() as conn,conn:
            conn.execute("CREATE TRIGGER fail_artifact BEFORE INSERT ON source_package_artifacts BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.acq.seal(c,raw(p),blobs)
        self.assertEqual(self.sql('SELECT * FROM source_packages'),[])
        self.assertEqual(self.sql('SELECT status FROM source_fetch_runs')[0][0],'fetching')

    def test_reset_no_fetch_refresh_new_revision(self):
        p=self.setup_source(); self.seal(p); self.processor.process(self.bid,'w')
        before=self.sql('SELECT * FROM source_packages')
        self.send('reset')
        self.assertEqual(self.sql('SELECT * FROM source_packages'),before)
        self.assertIsNone(self.acq.claim(self.bid,self.sid,'a'))
        self.store.change(self.task.id,'u',self.store.get(self.task.id).version,lambda t:setattr(t,'confirmation',{'mode':'formal','version':t.version,'user_id':'u'}))
        r=self.send('refresh_notion_source',{'source_id':self.sid})['data']
        self.assertEqual(r['revision'],2)
        self.assertIsNone(self.task.confirmation)
        self.assertFalse(self.task.sources[0].readable)
        self.send('finish_collection')
        q=copy.deepcopy(p); q.update(package_id=uuid4().hex,batch_id=r['batch_id'],revision=2)
        q['nodes'][0]['text']='New revision'
        self.seal(q); self.processor.process(r['batch_id'],'w')
        self.assertEqual(self.store.get(self.task.id).sources[0].content,'New revision')
        self.assertEqual(len(self.sql('SELECT * FROM source_packages')),2)
        self.assertEqual(self.processor.result(self.bid)[1][0]['blocks'][0]['text'],'Root title')

    def test_refresh_during_processing_rejected(self):
        self.setup_source()
        self.send('refresh_notion_source',{'source_id':self.sid},ok=False)

    def test_partial_and_failed_gate_and_coverage(self):
        p=self.setup_source(); p['outcome']='partial'; p['scope']['pagination_complete']=False
        self.seal(p); self.assertEqual(self.processor.process(self.bid,'w')['status'],'partial')
        t=self.store.get(self.task.id)
        self.assertFalse(check_coverage(t).overall_complete)
        self.assertIsNone(check_coverage(t).known_path_percent)
        with self.assertRaises(GenerationBlocked) as caught: gate(t,'formal')
        self.assertIn('Critical source incomplete',caught.exception.reasons)

    def test_failed_package_allows_processing_failure(self):
        p=self.setup_source(); p.update(outcome='failed',nodes=[],gaps=[dict(code='resource_limit')])
        self.seal(p)
        self.assertEqual(self.processor.process(self.bid,'w')['status'],'failed')

    def test_batch_source_limit(self):
        for _ in range(50): self.send('append_notion_reference',{'reference':ROOT})
        self.send('append_notion_reference',{'reference':ROOT},ok=False)
        self.assertEqual(len(self.store.get(self.task.id).sources),50)


    def test_default_failure_receipt_derives_internal_issue(self):
        self.setup_source()
        c=self.acq.claim(self.bid,self.sid,'controller')
        self.acq.transition(c,'fetching')
        self.acq.seal_failure(c)
        self.assertEqual(self.processor.process(self.bid,'w')['status'],'failed')
        result=self.processor.result(self.bid)[1][0]
        self.assertIn('acquisition_failed',[i['code'] for i in result['issues']])

    def test_failure_receipt_after_resource_rejection(self):
        p=self.setup_source()
        c=self.acq.claim(self.bid,self.sid,'controller'); self.acq.transition(c,'fetching')
        p['nodes'][0]['text']='x'*65537
        with self.assertRaises(ValueError): self.acq.seal(c,raw(p),{})
        self.acq.seal_failure(c,'resource_limit')
        self.assertEqual(self.processor.process(self.bid,'w')['status'],'failed')
        self.assertEqual(self.sql('SELECT outcome FROM source_fetch_runs')[0][0],'failed')


    def test_attachment_blob_immutability(self):
        p=self.setup_source(); blobs=artifact(p,doc(),'docx'); self.seal(p,blobs)
        with self.store._locked() as conn, conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE source_package_artifacts SET body=X'00'")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE source_packages SET content_fingerprint='forged'")
        self.assertEqual(self.processor.process(self.bid,'w')['status'],'completed')




class Migration3Tests(unittest.TestCase):
    def test_two_to_three_and_rollback(self):
        base=PROJECT/'.test-runtime'; base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as directory:
            store=Store(Path(directory)/'db')
            task=store.create('u')
            with store._locked() as conn,conn:
                for table in ('source_fetch_runs','source_packages','source_package_artifacts','batch_input_manifests'):
                    conn.execute('DROP TABLE '+table)
                for column in ('acquisition_status','input_manifest_digest','inputs_sealed_at'):
                    conn.execute('ALTER TABLE collection_batches DROP COLUMN '+column)
                conn.execute('PRAGMA user_version=2')
                conn.execute('CREATE TABLE source_packages(dummy TEXT)')
            with store._locked() as conn:
                with self.assertRaises(sqlite3.OperationalError): migrate(conn)
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],2)
                self.assertNotIn('acquisition_status',[r[1] for r in conn.execute('PRAGMA table_info(collection_batches)')])
                self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='source_fetch_runs'").fetchone())
                conn.execute('DROP TABLE source_packages'); conn.commit()
                migrate(conn); migrate(conn)
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],3)
            self.assertEqual(Store(store.root).get(task.id).dumps(),task.dumps())

    def test_zero_and_one_through_three_failure_rolls_back_all_steps(self):
        base=PROJECT/'.test-runtime'; base.mkdir(exist_ok=True)
        for version in (0,1):
            with tempfile.TemporaryDirectory(dir=base) as directory:
                store=Store(Path(directory)/'db')
                store.create('u')
                with store._locked() as conn,conn:
                    for table in ('source_fetch_runs','source_packages','source_package_artifacts','batch_input_manifests',
                                  'source_inputs','processing_runs','sanitized_contents','batch_manifests','processing_staging'):
                        conn.execute('DROP TABLE '+table)
                    for column in ('acquisition_status','input_manifest_digest','inputs_sealed_at','processing_status'):
                        conn.execute('ALTER TABLE collection_batches DROP COLUMN '+column)
                    if version==0:
                        for table in ('requests','events','collection_batches','artifacts'):
                            conn.execute('DROP TABLE '+table)
                    conn.execute('PRAGMA user_version='+str(version))
                    conn.execute('CREATE TABLE source_packages(dummy TEXT)')
                with store._locked() as conn:
                    before=conn.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()
                    with self.assertRaises(sqlite3.OperationalError): migrate(conn)
                    self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],version)
                    self.assertEqual(conn.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall(),before)
                    self.assertEqual(conn.execute('SELECT count(*) FROM history').fetchone()[0],1)


class ResourceBoundaryTests(unittest.TestCase):
    def test_actual_visited_blocks_and_attachments(self):
        p=package()
        p['nodes'] += [dict(id='n'+str(i),type='divider',parent_id=ROOT) for i in range(19999)]
        n.load_package(raw(p))
        p['nodes'].append(dict(id='overflow',type='divider',parent_id=ROOT))
        with self.assertRaises(ValueError): n.load_package(raw(p))
        p=package()
        for i in range(50): artifact(p,b'','pdf','a'+str(i))
        n.load_package(raw(p))
        artifact(p,b'','pdf','overflow')
        with self.assertRaises(ValueError): n.load_package(raw(p))

    def test_attachment_raw_and_total_declared_sizes(self):
        p=package()
        for i in range(10):
            artifact(p,b'','pdf','a'+str(i))
            p['artifacts'][-1]['size']=10*1024*1024
        n.load_package(raw(p))
        p['artifacts'][0]['size']+=1
        with self.assertRaises(ValueError): n.load_package(raw(p))
        p['artifacts'][0]['size']-=1
        artifact(p,b'x','pdf','overflow')
        with self.assertRaises(ValueError): n.load_package(raw(p))

    def test_actual_combined_text_budget_utf8(self):
        p=package()
        p['nodes'][0]['text']=''
        p['nodes'] += [dict(id='n'+str(i),type='paragraph',parent_id=ROOT,text='x'*65536) for i in range(128)]
        n.load_package(raw(p))
        p['nodes'][0]['text']='x'
        with self.assertRaises(ValueError): n.load_package(raw(p))
        p=package()
        p['nodes'][0]['text']='中'*21846
        with self.assertRaises(ValueError): n.load_package(raw(p))

    def test_non_string_enum_values_rejected(self):
        for key in ('outcome','root'):
            p=package()
            if key=='root': p['root']['type']={}
            else: p[key]={}
            with self.assertRaises(ValueError): n.load_package(raw(p))
