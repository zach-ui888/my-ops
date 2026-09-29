"""Bounded offline parsers. Never resolve relationships or write archive members."""
from io import BytesIO
import posixpath
import re
import stat
import warnings
import zipfile
import zlib

ENTRY_COUNT = 2000
ENTRY_BYTES = 32 * 1024 * 1024
TOTAL_BYTES = 100 * 1024 * 1024
RATIO = 100
XML_DEPTH = 64
XML_NODES = 100000
PDF_PAGES = 300
TEXT_BYTES = 64 * 1024
OUTPUT_BYTES = 8 * 1024 * 1024
SHEETS = 50
ROWS = 50000
COLS = 512
CELLS = 500000
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
S = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'


class Unsafe(ValueError):
    pass


def require(ok, code):
    if not ok:
        raise Unsafe(code)


def xml(data):
    from defusedxml.ElementTree import iterparse
    depth = nodes = 0
    root = None
    for event, elem in iterparse(BytesIO(data), events=('start', 'end'),
                                 forbid_dtd=True, forbid_entities=True, forbid_external=True):
        if event == 'start':
            root = elem if root is None else root
            depth += 1
            nodes += 1
            require(depth <= XML_DEPTH and nodes <= XML_NODES, 'xml_resource_limit')
        else:
            depth -= 1
    return root


def package(blob):
    require(blob.startswith(b'PK\x03\x04'), 'signature_mismatch')
    parts = {}
    with zipfile.ZipFile(BytesIO(blob)) as archive:
        entries = archive.infolist()
        require(len(entries) <= ENTRY_COUNT, 'zip_entry_limit')
        total = 0
        seen = set()
        for entry in entries:
            name = entry.filename.replace('\\', '/')
            normalized = posixpath.normpath(name)
            require(not name.startswith('/') and not re.match(r'^[A-Za-z]:', name)
                    and '..' not in name.split('/') and '\x00' not in name, 'zip_path_rejected')
            require(normalized not in seen, 'zip_duplicate_entry')
            seen.add(normalized)
            require(not stat.S_ISLNK(entry.external_attr >> 16), 'zip_symlink')
            require(not entry.flag_bits & 1, 'zip_encrypted')
            total += entry.file_size
            require(entry.file_size <= ENTRY_BYTES and total <= TOTAL_BYTES, 'zip_size_limit')
            require(entry.file_size <= max(1, entry.compress_size) * RATIO, 'zip_ratio_limit')
            require(entry.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED), 'zip_compression')
            with archive.open(entry) as stream:
                data = stream.read(min(ENTRY_BYTES, entry.file_size) + 1)
            require(len(data) == entry.file_size, 'zip_size_limit')
            parts[normalized] = data
    require('[Content_Types].xml' in parts and '_rels/.rels' in parts, 'invalid_ooxml')
    # Validate every XML part, including ignored parts. Nested archives remain inert bytes.
    trees = {name: xml(data) for name, data in parts.items()
             if name.endswith(('.xml', '.rels'))}
    types = trees['[Content_Types].xml']
    require(types.tag == '{http://schemas.openxmlformats.org/package/2006/content-types}Types', 'invalid_ooxml')
    require(not any('macroEnabled' in e.get('ContentType', '') for e in types), 'macro_format_rejected')
    require(not any('vbaproject' in name.lower() for name in parts), 'macro_format_rejected')
    return parts, trees


class Output:
    def __init__(self, result):
        self.result = result
        self.size = 0

    def gap(self, code, **where):
        issue = dict(code=code, severity='warning', locator=dict(self.result['locator'], **where))
        if issue not in self.result['issues']:
            self.result['issues'].append(issue)

    def block(self, text, **where):
        if not text.strip():
            return
        size = len(text.encode('utf-8'))
        self.size += size
        require(size <= TEXT_BYTES and self.size <= OUTPUT_BYTES and
                len(self.result['blocks']) < 20000, 'text_resource_limit')
        self.result['blocks'].append(dict(type='text', text=text, trust='untrusted_source_data',
                                          locator=dict(self.result['locator'], **where)))


def ooxml(blob, kind, out):
    parts, trees = package(blob)
    main = 'word/document.xml' if kind == 'docx' else 'xl/workbook.xml'
    expected = ('application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'
                if kind == 'docx' else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml')
    require(main in trees and any(e.get('PartName') == '/' + main and e.get('ContentType') == expected
                                 for e in trees['[Content_Types].xml']), 'container_mismatch')
    for name, tree in trees.items():
        if name.endswith('.rels'):
            for rel in tree:
                if rel.get('TargetMode') == 'External':
                    out.gap('external_relationship_unresolved')
    for name in parts:
        if any(token in name.lower() for token in ('drawing', 'media/', 'embedding', 'externallink', 'diagram', 'activex', 'charts/', 'pivot', 'customxml', 'connections')):
            out.gap('complex_component_unresolved')
    if kind == 'docx':
        require(trees[main].tag == W + 'document', 'container_mismatch')
        for elem in trees[main].iter():
            local = elem.tag.split('}')[-1]
            if local in {'drawing', 'pict', 'object', 'txbxContent', 'ins', 'del', 'moveFrom', 'moveTo',
                         'altChunk', 'fldSimple', 'instrText', 'sdt', 'AlternateContent', 'sym', 'oMath', 'oMathPara', 'subDoc', 'contentPart'}:
                out.gap('docx_complex_unresolved')
        for name in parts:
            if name.startswith(('word/header', 'word/footer', 'word/footnotes', 'word/endnotes', 'word/comments')):
                out.gap('docx_auxiliary_unresolved')
        for i, paragraph in enumerate(trees[main].iter(W + 'p'), 1):
            text = ''.join(e.text or '' if e.tag == W + 't' else '\t' if e.tag == W + 'tab' else '\n'
                           for e in paragraph.iter() if e.tag in {W + 't', W + 'tab', W + 'br', W + 'cr'})
            out.block(text, part=main, paragraph=i)
        return
    require(trees[main].tag == S + 'workbook', 'container_mismatch')
    shared = []
    if 'xl/sharedStrings.xml' in trees:
        for item in trees['xl/sharedStrings.xml'].findall(S + 'si'):
            value = ''.join(t.text or '' for t in item.iter(S + 't'))
            require(len(value.encode()) <= TEXT_BYTES, 'cell_text_limit')
            shared.append(value)
    rels = trees.get('xl/_rels/workbook.xml.rels')
    require(rels is not None, 'invalid_ooxml')
    mapping = {r.get('Id'): r for r in rels}
    sheets = trees[main].findall(S + 'sheets/' + S + 'sheet')
    require(len(sheets) <= SHEETS, 'sheet_limit')
    count = 0
    used = set()
    for sheet in sheets:
        name = sheet.get('name', '')
        hidden = sheet.get('state', 'visible')
        rel = mapping.get(sheet.get(R + 'id'))
        require(rel is not None and rel.get('TargetMode') != 'External', 'invalid_sheet_relationship')
        target = rel.get('Target', '')
        require('\\' not in target and '..' not in target.split('/'), 'invalid_sheet_relationship')
        path = target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/' + target)
        require(path.startswith('xl/') and path in trees and path not in used, 'invalid_sheet_relationship')
        used.add(path)
        tree = trees[path]
        require(tree.tag == S + 'worksheet', 'invalid_sheet_relationship')
        out.gap('hidden_sheet', sheet=name, sheet_state=hidden) if hidden != 'visible' else None
        if any(e.tag.split('}')[-1] in {'drawing', 'legacyDrawing', 'oleObjects', 'extLst', 'mergeCells'} for e in tree.iter()):
            out.gap('xlsx_component_unresolved', sheet=name)
        rows = tree.findall(S + 'sheetData/' + S + 'row')
        require(len(rows) <= ROWS, 'row_limit')
        seen = set()
        for row in rows:
            require(1 <= int(row.get('r', '0')) <= ROWS, 'row_limit')
            for cell in row.findall(S + 'c'):
                count += 1
                require(count <= CELLS, 'cell_limit')
                ref = cell.get('r', '')
                match = re.fullmatch(r'([A-Z]+)([1-9][0-9]*)', ref)
                require(match is not None and ref not in seen, 'invalid_cell')
                seen.add(ref)
                col = 0
                for char in match[1]:
                    col = col * 26 + ord(char) - 64
                require(col <= COLS and int(match[2]) <= ROWS, 'cell_coordinate_limit')
                where = dict(sheet=name, cell=ref, sheet_state=hidden)
                formula = cell.find(S + 'f')
                value = cell.findtext(S + 'v', '')
                if formula is not None:
                    out.block(formula.text or '[shared formula unresolved]', value_kind='formula', **where)
                    out.gap('formula_not_evaluated', **where)
                    if value:
                        out.block(value, value_kind='cached_value', **where)
                else:
                    if cell.get('t') == 's':
                        index = int(value)
                        require(0 <= index < len(shared), 'invalid_shared_string')
                        value = shared[index]
                    elif cell.get('t') == 'inlineStr':
                        value = ''.join(t.text or '' for t in cell.iter(S + 't'))
                    out.block(value, value_kind='value', **where)


def pdf(blob, out):
    require(blob.startswith(b'%PDF-'), 'signature_mismatch')
    from pypdf import PdfReader
    reader = PdfReader(BytesIO(blob), strict=True)
    require(not reader.is_encrypted, 'encrypted_pdf')
    require(len(reader.pages) <= PDF_PAGES, 'pdf_page_limit')
    for number, page in enumerate(reader.pages, 1):
        # Any graphics/resources are conservatively unresolved; no rendering or fetching.
        resources = page.get('/Resources', {})
        resources = resources.get_object() if hasattr(resources, 'get_object') else resources
        fonts = resources.get('/Font', {})
        fonts = fonts.get_object() if hasattr(fonts, 'get_object') else fonts
        if any(font.get_object().get('/Subtype') == '/Type3' for font in fonts.values()):
            out.gap('visual_unresolved', page=number)
        if resources.get('/XObject') or page.get('/Annots'):
            out.gap('visual_unresolved', page=number)
        # Preflight compressed page streams before pypdf constructs/decompresses content.
        # Unsupported filter chains fail closed. This bounds Flate expansion independently.
        streams = page.get('/Contents')
        if streams is not None:
            streams = streams.get_object()
            streams = streams if isinstance(streams, list) else [streams]
            expanded = 0
            for stream in streams:
                stream = stream.get_object()
                raw = stream._data
                require(isinstance(raw, bytes) and len(raw) <= ENTRY_BYTES, 'pdf_stream_limit')
                filters = stream.get('/Filter')
                if filters is not None:
                    filters = filters.get_object()
                    filters = filters if isinstance(filters, list) else [filters]
                    require(list(filters) == ['/FlateDecode'], 'pdf_unsupported_filter')
                    decoder = zlib.decompressobj()
                    decoded = decoder.decompress(raw, ENTRY_BYTES + 1)
                    require(len(decoded) <= ENTRY_BYTES and decoder.eof and not decoder.unconsumed_tail, 'pdf_stream_limit')
                    expanded += len(decoded)
                else:
                    expanded += len(raw)
                require(expanded <= ENTRY_BYTES, 'pdf_stream_limit')
        content = page.get_contents()
        if content is not None:
            require(len(content.get_data()) <= ENTRY_BYTES, 'pdf_stream_limit')
            if any(op in {b'Do', b'BI', b'S', b's', b'f', b'F', b'f*', b'B', b'b', b'B*', b'b*', b'sh', b'INLINE IMAGE'} for _, op in content.operations):
                out.gap('visual_unresolved', page=number)
        text = page.extract_text() or ''
        if not text.strip():
            out.gap('text_layer_missing', page=number)
        out.block(text, page=number)
    root = reader.trailer['/Root']
    if any(key in root for key in ('/Names', '/OpenAction', '/AA', '/AcroForm')):
        out.gap('pdf_active_or_auxiliary_content_unresolved')


def picture(blob, kind, out):
    expected = 'PNG' if kind == 'png' else 'JPEG'
    require(blob.startswith(b'\x89PNG\r\n\x1a\n') if kind == 'png' else blob.startswith(b'\xff\xd8\xff'), 'signature_mismatch')
    from PIL import Image
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(BytesIO(blob)) as image:
            require(image.format == expected, 'signature_mismatch')
            width, height = image.size
            require(0 < width <= 20000 and 0 < height <= 20000 and width * height <= 25000000, 'image_resource_limit')
            image.verify()
        with Image.open(BytesIO(blob)) as image:
            image.load()
    # Descriptor only: no EXIF, metadata, pixels or inferred semantics enter text blocks.
    out.gap('visual_unresolved', image_format=expected, width=width, height=height)


def parse_complex(result, blob, kind):
    out = Output(result)
    try:
        require(len(blob) <= 10 * 1024 * 1024, 'source_size_limit')
        if kind == 'pdf':
            pdf(blob, out)
        elif kind in {'docx', 'xlsx'}:
            ooxml(blob, kind, out)
        else:
            picture(blob, kind, out)
        if not result['blocks'] and not result['issues']:
            out.gap('empty_content')
        result.update(status='partial' if result['issues'] else 'completed',
                      completeness='incomplete' if result['issues'] else 'complete')
    except Exception as error:
        # Only our fixed internal codes cross the parser boundary; library messages may contain paths.
        code = str(error) if isinstance(error, Unsafe) else 'parser_dependency_missing' if isinstance(error, ImportError) else 'unsafe_or_invalid_content'
        result.update(status='failed', completeness='incomplete', blocks=[],
                      issues=[dict(code=code, severity='error', locator=result['locator'])])
    return result
