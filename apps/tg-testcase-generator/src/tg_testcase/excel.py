"""Lossless template-based XLSX exporter using only the standard library.

Only worksheet dimension/data are rewritten. All other ZIP members and the
worksheet XML outside those elements are preserved, including namespace scope.
"""
from copy import deepcopy
from io import BytesIO
import re
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from .storage import inside_project

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
N = {"m": NS}
SHEET = "xl/worksheets/sheet1.xml"
HEADERS = ["用例ID", "模块", "测试用例描述", "前置条件", "测试步骤", "预期结果",
           "实际结果（测试环境）", "实际结果（预发环境）", "实际结果（灰度环境）",
           "实际结果（生产环境）", "是否通过", "备注"]
DRAFT = "【带缺口草稿：资料/规则存在缺口，不代表100%完整覆盖】"


def value(cell, shared):
    if cell.get("t") == "s":
        return shared[int(cell.find("m:v", N).text)]
    return "".join(cell.itertext())


def load(blob):
    with ZipFile(BytesIO(blob)) as z:
        members = {n: z.read(n) for n in z.namelist()}
    root = ET.fromstring(members[SHEET])
    strings = ET.fromstring(members["xl/sharedStrings.xml"])
    shared = ["".join(t.text or "" for t in si.findall(".//m:t", N)) for si in strings]
    return members, root, shared


def outer_xml(blob):
    text = blob.decode("utf-8")
    text = re.sub(r"<sheetData>.*?</sheetData>", "<sheetData/>", text, flags=re.S)
    return re.sub(r'<dimension ref="[^"]+"\s*/>', '<dimension/>', text)


def validate_export(template, output, cases, draft=False):
    original, base, shared = load(template)
    result, sheet, out_shared = load(output)
    if original.keys() != result.keys():
        raise ValueError("XLSX package structure changed")
    for name in original:
        if name != SHEET and original[name] != result[name]:
            raise ValueError(f"Template member changed: {name}")
    if outer_xml(original[SHEET]) != outer_xml(result[SHEET]):
        raise ValueError("Worksheet attributes changed")
    rows = sheet.findall("m:sheetData/m:row", N)
    header = base.find("m:sheetData/m:row", N)
    if ET.tostring(rows[0]) != ET.tostring(header) or len(rows) != len(cases) + 1:
        raise ValueError("Header/row count mismatch")
    if sheet.find("m:dimension", N).get("ref") != f"A1:L{len(cases) + 1}":
        raise ValueError("Dimension mismatch")
    styles = data_styles(base)
    for number, (row, case) in enumerate(zip(rows[1:], cases), 2):
        expected_row = {**base.findall("m:sheetData/m:row", N)[1].attrib, "r": str(number)}
        if row.attrib != expected_row:
            raise ValueError("Data row attributes changed")
        cells = row.findall("m:c", N)
        if len(cells) != 12 or [c.get("r") for c in cells] != [f"{c}{row.get('r')}" for c in "ABCDEFGHIJKL"]:
            raise ValueError("Must retain 12 columns")
        expected = fields(case, draft)
        for i, cell in enumerate(cells):
            if cell.get("s") != styles[i + 1]:
                raise ValueError("Cell style mismatch")
            if cell.find("m:f", N) is not None or cell.get("t") != "inlineStr":
                raise ValueError("Formula or non-text cell")
            if value(cell, out_shared) != expected[i]:
                raise ValueError("Cell content mismatch")
        if any(value(c, out_shared) for c in cells[6:]):
            raise ValueError("Execution results must be blank")
    return {"sheet": "测试用例模板", "columns": 12, "cases": len(cases),
            "template_attributes_preserved": True, "reopened": True}


def fields(case, draft):
    return [case.id, case.module, (DRAFT if draft else "") + case.description,
            case.preconditions, "\n".join(case.steps), "\n".join(case.expected)] + [""] * 6


def data_styles(base):
    styles = {}
    for col in base.findall("m:cols/m:col", N):
        for i in range(int(col.get("min")), min(12, int(col.get("max"))) + 1):
            styles[i] = col.get("style", "0")
    for cell in base.findall("m:sheetData/m:row", N)[1]:
        styles[ord(cell.get("r")[0]) - 64] = cell.get("s", "0")
    return styles


def render(template_path, cases, draft=False):
    template = inside_project(template_path).read_bytes()
    members, base, shared = load(template)
    workbook = ET.fromstring(members["xl/workbook.xml"])
    sheets = workbook.findall("m:sheets/m:sheet", N)
    rows = base.findall("m:sheetData/m:row", N)
    if len(sheets) != 1 or sheets[0].get("name") != "测试用例模板":
        raise ValueError("Unexpected template sheets")
    if len(rows) != 2 or [value(c, shared) for c in rows[0]] != HEADERS:
        raise ValueError("Unexpected template header/placeholder structure")
    if value(rows[1][0], shared) != "Z001":
        raise ValueError("Unexpected placeholder")
    if not cases or len(cases) > 1048575 or len({c.id for c in cases}) != len(cases):
        raise ValueError("Nonempty unique cases within Excel row limit required")
    styles = data_styles(base)
    data = ET.Element("sheetData")
    # Keep header raw below; only serialize new data rows without namespace prefixes.
    for number, case in enumerate(cases, 2):
        row = ET.SubElement(data, "row", {**rows[1].attrib, "r": str(number)})
        for i, text in enumerate(fields(case, draft), 1):
            if len(text.encode("utf-16-le")) // 2 > 32767 or any(
                ord(c) < 32 and c not in "\t\n\r" for c in text
            ):
                raise ValueError("Invalid Excel text length/control character")
            cell = ET.SubElement(row, "c", {"r": f"{chr(64+i)}{number}", "s": styles[i], "t": "inlineStr"})
            # inlineStr is a literal string even for =,+,-,@ and leading whitespace.
            t = ET.SubElement(ET.SubElement(cell, "is"), "t", {"xml:space": "preserve"})
            t.text = text
    original_xml = members[SHEET].decode("utf-8")
    header = re.search(r"<sheetData>(<row\b.*?</row>)", original_xml, re.S).group(1)
    serialized = ET.tostring(data, encoding="unicode")
    serialized = serialized.replace("<sheetData>", "<sheetData>" + header, 1)
    updated = re.sub(r"<sheetData>.*?</sheetData>", lambda _: serialized, original_xml, flags=re.S)
    updated = re.sub(r'<dimension ref="[^"]+"\s*/>', f'<dimension ref="A1:L{len(cases)+1}"/>', updated)
    stream = BytesIO()
    with ZipFile(BytesIO(template)) as source, ZipFile(stream, "w") as target:
        for info in source.infolist():
            target.writestr(deepcopy(info), updated.encode() if info.filename == SHEET else members[info.filename])
    blob = stream.getvalue()
    validate_export(template, blob, cases, draft)
    return blob
