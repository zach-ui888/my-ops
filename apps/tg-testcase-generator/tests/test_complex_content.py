"""Offline fixtures only; optional runtime dependencies must be installed to run all cases."""
import hashlib
import importlib.util
from io import BytesIO
import json
import stat
import time
import unittest
from unittest.mock import patch
import zipfile

from tg_testcase.content import parse_content
from tg_testcase import complex_content as c

HAS_XML = importlib.util.find_spec('defusedxml') is not None
HAS_PDF = importlib.util.find_spec('pypdf') is not None
HAS_IMAGE = importlib.util.find_spec('PIL') is not None


def parse(blob, kind):
    return parse_content(dict(source_id='s', origin='upload', sha256=hashlib.sha256(blob).hexdigest(),
                              byte_length=len(blob), revision=1, locator='source/s.' + kind, kind=kind), blob)


def archive(entries, compression=zipfile.ZIP_STORED):
    stream = BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=compression) as z:
        for name, data in entries:
            z.writestr(name, data)
    return stream.getvalue()


def office(kind, main, extra=()):
    path = 'word/document.xml' if kind == 'docx' else 'xl/workbook.xml'
    content = ('wordprocessingml.document' if kind == 'docx' else 'spreadsheetml.sheet')
    return archive([('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/' + path + '" ContentType="application/vnd.openxmlformats-officedocument.' + content + '.main+xml"/></Types>'),
                    ('_rels/.rels', '<Relationships/>'), (path, main), *extra])


def doc(body='<w:p><w:r><w:t>Hello</w:t></w:r></w:p>', extra=()):
    return office('docx', '<w:document xmlns:w="' + c.W[1:-1] + '"><w:body>' + body + '</w:body></w:document>', extra)


def book(cells='<c r="A1" t="inlineStr"><is><t>Hello</t></is></c>', state='visible', extra=()):
    return office('xlsx', '<workbook xmlns="' + c.S[1:-1] + '" xmlns:r="' + c.R[1:-1] + '"><sheets><sheet name="Main" state="' + state + '" r:id="r1"/></sheets></workbook>',
                  [('xl/_rels/workbook.xml.rels', '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>'),
                   ('xl/worksheets/sheet1.xml', '<worksheet xmlns="' + c.S[1:-1] + '"><sheetData><row r="1">' + cells + '</row></sheetData></worksheet>'), *extra])


class ArchiveTests(unittest.TestCase):
    def rejected(self, blob, code):
        with self.assertRaises(c.Unsafe) as caught:
            c.package(blob)
        self.assertEqual(str(caught.exception), code)

    def test_traversal_absolute_drive(self):
        for name in ['../evil', '/evil', 'C:/evil', 'a/../../evil', 'a\\..\\evil']:
            self.rejected(archive([(name, 'x')]), 'zip_path_rejected')

    def test_symlink(self):
        entry = zipfile.ZipInfo('link')
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.rejected(archive([(entry, 'target')]), 'zip_symlink')

    def test_duplicate_normalized(self):
        self.rejected(archive([('a/b', 'x'), ('a/./b', 'y')]), 'zip_duplicate_entry')

    def test_entry_count(self):
        with patch.object(c, 'ENTRY_COUNT', 1):
            self.rejected(archive([('a', ''), ('b', '')]), 'zip_entry_limit')

    def test_entry_size(self):
        with patch.object(c, 'ENTRY_BYTES', 2):
            self.rejected(archive([('a', '123')]), 'zip_size_limit')

    def test_total_size(self):
        with patch.object(c, 'TOTAL_BYTES', 3):
            self.rejected(archive([('a', '12'), ('b', '34')]), 'zip_size_limit')

    def test_ratio(self):
        self.rejected(archive([('a', '0' * 100000)], zipfile.ZIP_DEFLATED), 'zip_ratio_limit')

    def test_signature(self):
        for kind in ['docx', 'xlsx', 'pdf']:
            result = parse(b'fake', kind)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['issues'][0]['code'], 'signature_mismatch')

    def test_exception_redacted(self):
        with patch.object(c, 'pdf', side_effect=RuntimeError('/private/server/secret credential')):
            result = parse(b'%PDF-', 'pdf')
        self.assertNotIn('/private', json.dumps(result))
        self.assertEqual(result['issues'][0]['code'], 'unsafe_or_invalid_content')


@unittest.skipUnless(HAS_XML, 'defusedxml runtime dependency unavailable')
class OfficeTests(unittest.TestCase):
    def test_doc_paragraph_table(self):
        result = parse(doc('<w:p><w:r><w:t>First</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'), 'docx')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([b['text'] for b in result['blocks']], ['First', 'Cell'])
        self.assertEqual(result['blocks'][1]['locator']['paragraph'], 2)

    def test_complex_doc(self):
        for tag in ['drawing', 'pict', 'ins', 'del', 'txbxContent', 'object', 'altChunk']:
            result = parse(doc('<w:p><w:r><w:t>Known</w:t></w:r><w:' + tag + '/></w:p>'), 'docx')
            self.assertEqual(result['status'], 'partial')

    def test_dtd_entities(self):
        for data in [b'<!DOCTYPE a><a/>', b'<!DOCTYPE a [<!ENTITY x "test">]><a>&x;</a>', b'<!DOCTYPE a SYSTEM "https://invalid.example"><a/>']:
            self.assertEqual(parse(doc(extra=[('word/ignored.xml', data)]), 'docx')['status'], 'failed')

    def test_xml_limits(self):
        for constant, limit in [('XML_DEPTH', 2), ('XML_NODES', 3)]:
            with patch.object(c, constant, limit):
                self.assertEqual(parse(doc(), 'docx')['status'], 'failed')

    def test_nested_not_expanded(self):
        result = parse(doc(extra=[('word/embeddings/nested.zip', archive([('../evil', 'x')]))]), 'docx')
        self.assertEqual(result['status'], 'partial')

    def test_container_mismatch(self):
        self.assertEqual(parse(doc(), 'xlsx')['status'], 'failed')

    def test_sheet_values_hidden(self):
        result = parse(book(state='hidden'), 'xlsx')
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['blocks'][0]['locator'], dict(path='source/s.xlsx', source_id='s', sheet='Main', cell='A1', sheet_state='hidden', value_kind='value'))

    def test_formula_cached(self):
        result = parse(book('<c r="A1"><f>1+2</f><v>3</v></c>'), 'xlsx')
        self.assertEqual([b['locator']['value_kind'] for b in result['blocks']], ['formula', 'cached_value'])
        self.assertEqual(result['status'], 'partial')

    def test_xlsx_limits(self):
        for key in ['SHEETS', 'ROWS', 'COLS', 'CELLS', 'TEXT_BYTES']:
            with patch.object(c, key, 0):
                self.assertEqual(parse(book(), 'xlsx')['status'], 'failed', key)

    def test_external_drawing(self):
        for name, data in [('xl/drawings/drawing.xml', '<drawing/>'), ('xl/externalLinks/link.rels', '<Relationships><Relationship TargetMode="External" Target="https://invalid.example"/></Relationships>')]:
            self.assertEqual(parse(book(extra=[(name, data)]), 'xlsx')['status'], 'partial')

    def test_no_external_effects(self):
        with patch('socket.socket', side_effect=AssertionError('network')), patch('subprocess.Popen', side_effect=AssertionError('shell')):
            self.assertEqual(parse(doc(), 'docx')['status'], 'completed')
            self.assertEqual(parse(book(), 'xlsx')['status'], 'completed')


@unittest.skipUnless(HAS_PDF, 'pypdf runtime dependency unavailable')
class PDFTests(unittest.TestCase):
    def fixture(self, text=True, encrypt=False, visual=False):
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        writer = PdfWriter()
        page = writer.add_blank_page(width=200, height=200)
        if text:
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(b'BT /F1 12 Tf 10 100 Td (Hello) Tj ET' + (b' 0 0 10 10 re f' if visual else b''))
            page[NameObject('/Contents')] = writer._add_object(stream)
        if encrypt:
            writer.encrypt('test-only')
        buf = BytesIO()
        writer.write(buf)
        return buf.getvalue()

    def test_text_page(self):
        result = parse(self.fixture(), 'pdf')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['blocks'][0]['locator']['page'], 1)
        self.assertIn('Hello', result['blocks'][0]['text'])

    def test_scanned_empty(self):
        self.assertEqual(parse(self.fixture(False), 'pdf')['status'], 'partial')

    def test_mixed_visual(self):
        result = parse(self.fixture(visual=True), 'pdf')
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['blocks'])

    def test_encrypted_broken(self):
        self.assertEqual(parse(self.fixture(encrypt=True), 'pdf')['issues'][0]['code'], 'encrypted_pdf')
        self.assertEqual(parse(b'%PDF-broken', 'pdf')['status'], 'failed')

    def test_page_text_limits(self):
        for key in ['PDF_PAGES', 'TEXT_BYTES', 'OUTPUT_BYTES', 'ENTRY_BYTES']:
            with patch.object(c, key, 0):
                self.assertEqual(parse(self.fixture(), 'pdf')['status'], 'failed')


@unittest.skipUnless(HAS_IMAGE, 'Pillow runtime dependency unavailable')
class ImageTests(unittest.TestCase):
    def fixture(self, fmt):
        from PIL import Image
        out = BytesIO()
        Image.new('RGB', (10, 10)).save(out, format=fmt)
        return out.getvalue()

    def test_png_jpeg(self):
        for kind, fmt in [('png', 'PNG'), ('jpg', 'JPEG'), ('jpeg', 'JPEG')]:
            result = parse(self.fixture(fmt), kind)
            self.assertEqual(result['status'], 'partial')
            self.assertEqual(result['blocks'], [])
            self.assertEqual(result['issues'][0]['code'], 'visual_unresolved')
            self.assertEqual(result['issues'][0]['locator']['width'], 10)

    def test_mismatch(self):
        self.assertEqual(parse(self.fixture('PNG'), 'jpg')['status'], 'failed')

    def test_bomb(self):
        from PIL import Image
        with patch.object(Image, 'MAX_IMAGE_PIXELS', 1):
            self.assertEqual(parse(self.fixture('PNG'), 'png')['status'], 'failed')

    def test_dimension(self):
        from PIL import Image
        out = BytesIO()
        Image.new('RGB', (20001, 1)).save(out, format='PNG')
        self.assertEqual(parse(out.getvalue(), 'png')['status'], 'failed')
