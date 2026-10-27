#!/usr/bin/env python3
"""Opt-in native engine acceptance on copies; no GUI control or listening port.

The build-only upstream CLIs exercise the same engines as the installed apps.
This does not establish keyboard, clipboard, Strata, printing or GUI acceptance.
"""
import argparse
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET
import zipfile


def run(binary, *args):
    result = subprocess.run([str(binary), *map(str, args)], capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError(f'{binary.name} {args}: {result.returncode}\n{result.stdout}\n{result.stderr}')
    return result.stdout


def objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values(): yield from objects(child)
    elif isinstance(value, list):
        for child in value: yield from objects(child)


def docx_format(path):
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read('word/document.xml'))
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        assert root.findall('.//w:tbl', ns) and root.findall('.//w:b', ns), 'Table/bold lost in DOCX round trip'


def writer(binary, out):
    source = out / 'input.html'
    source.write_text('<html><body><h1>GREYWARD acceptance</h1><p><b>Bold text</b> and ordinary text.</p>'
                      '<table><tr><td>Item</td><td>Value</td></tr><tr><td>Office</td><td>42</td></tr></table></body></html>')
    document = out / 'writer.docx'
    run(binary, 'convert', source, document)
    text = run(binary, 'text', document)
    assert 'GREYWARD acceptance' in text and 'Office' in text and '42' in text, text
    docx_format(document)
    reopened = out / 'writer-reopened.docx'
    run(binary, 'convert', document, reopened)
    assert 'GREYWARD acceptance' in run(binary, 'text', reopened)
    docx_format(reopened)
    pdf = out / 'writer.pdf'
    run(binary, 'convert', reopened, pdf)
    assert pdf.read_bytes().startswith(b'%PDF-')
    run(binary, 'render', reopened, out / 'writer.png', '--scale', '1')
    return {'docx_table_bold_save_reopen_pdf_render': True}


def sheets(binary, out):
    csv = out / 'input.csv'
    csv.write_text('Quantity,Price,Total\n3,7,\n2,9,\n')
    imported = out / 'imported.xlsx'
    run(binary, 'convert', csv, imported)
    with zipfile.ZipFile(imported) as archive:
        first = ET.fromstring(archive.read('xl/workbook.xml')).find('{*}sheets/{*}sheet').get('name')
    book = out / 'sheets.xlsx'
    run(binary, 'run', '--in', imported, '--cmd', 'cell.set=' + json.dumps({'cell': 'C2', 'input': '=A2*B2'}),
        '--cmd', 'home.insertSheet=' + json.dumps({'name': 'Summary'}), '--out', book)
    with zipfile.ZipFile(book) as archive:
        workbook = ET.fromstring(archive.read('xl/workbook.xml'))
        assert len(workbook.findall('{*}sheets/{*}sheet')) == 2, 'Second worksheet lost'
        assert any(b'A2*B2' in archive.read(n) for n in archive.namelist() if n.startswith('xl/worksheets/sheet'))
    values = run(binary, 'cat', book, '--sheet', first, '--range', 'A1:C3', '--csv')
    assert '21' in values, values
    exported = out / 'exported.csv'
    run(binary, 'convert', book, exported, '--sheet', first)
    assert '21' in exported.read_text(), exported.read_text()
    reopened = out / 'sheets-reopened.xlsx'
    run(binary, 'convert', book, reopened)
    assert '21' in run(binary, 'cat', reopened, '--sheet', first, '--range', 'C2:C2', '--csv')
    return {'xlsx_formula_two_sheets_save_reopen_csv': True}


def pdf_fixture(path):
    stream = b'BT /F1 18 Tf 60 740 Td (GREYWARD acceptance) Tj ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>']
    data = bytearray(b'%PDF-1.4\n'); offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data)); data.extend(str(index).encode() + b' 0 obj\n' + obj + b'\nendobj\n')
    xref = len(data)
    data.extend(b'xref\n0 7\n0000000000 65535 f \n')
    for offset in offsets[1:]: data.extend(f'{offset:010d} 00000 n \n'.encode())
    data.extend(f'trailer\n<< /Size 7 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    path.write_bytes(data)


def pdf(binary, out):
    source, saved = out / 'input.pdf', out / 'edited.pdf'
    pdf_fixture(source)
    assert 'GREYWARD acceptance' in run(binary, 'text', source)
    steps = [
        {'tool': 'doc_open', 'args': {'path': str(source)}},
        {'tool': 'text_find', 'args': {'doc': 1, 'query': 'GREYWARD'}},
        {'tool': 'comment_add', 'args': {'doc': 1, 'page': 1, 'type': 'note', 'at': [60, 140], 'contents': 'GREYWARD annotation'}},
        {'tool': 'form_add_field', 'args': {'doc': 1, 'page': 1, 'type': 'text', 'rect': [60, 180, 240, 210], 'name': 'Acceptance'}},
        {'tool': 'form_fill', 'args': {'doc': 1, 'values': {'Acceptance': 'Verified'}}},
        {'tool': 'page_rotate', 'args': {'doc': 1, 'pages': [2], 'degrees': 90}},
        {'tool': 'doc_save', 'args': {'doc': 1, 'path': str(saved)}},
        {'tool': 'doc_close', 'args': {'doc': 1}},
        {'tool': 'doc_open', 'args': {'path': str(saved)}},
        {'tool': 'form_fields', 'args': {'doc': 2}},
        {'tool': 'comment_list', 'args': {'doc': 2}},
        {'tool': 'doc_info', 'args': {'doc': 2}},
    ]
    script = out / 'steps.json'; script.write_text(json.dumps(steps))
    report = run(binary, 'run', '--script', script, '--report')
    (out / 'readback.json').write_text(report)
    result = json.loads(report)
    assert result['ok'] and all(step['status'] == 'ok' for step in result['steps']), report
    assert any(item.get('matches') for item in objects(result['steps'][1]['result'])), 'Search found no text'
    # Inspect only results produced after closing and reopening the saved file.
    fields, comments, info = [step['result'] for step in result['steps'][-3:]]
    assert 'Acceptance' in json.dumps(fields) and 'Verified' in json.dumps(fields), 'Saved form value lost'
    assert 'GREYWARD annotation' in json.dumps(comments), 'Saved annotation lost'
    assert any(item.get('rotation') == 90 for item in objects(info)), 'Saved page rotation lost'
    run(binary, 'render', saved, '--page', '1', '--out', out / 'pdf.png')
    return {'pdf_read_search_annotation_form_page_edit_save_reopen_render': True}


def slides(binary, out):
    presentation = out / 'slides.pptx'
    run(binary, 'run', '--cmd', 'slide.new=' + json.dumps({'layout': 'titleOnly', 'title': 'GREYWARD acceptance'}),
        '--cmd', 'shape.insert=' + json.dumps({'preset': 'roundRect', 'rect': [80, 160, 320, 120], 'text': 'Editable content'}),
        '--save', presentation)
    assert 'GREYWARD acceptance' in run(binary, 'info', presentation)
    reopened = out / 'slides-reopened.pptx'
    run(binary, 'convert', presentation, reopened)
    assert 'GREYWARD acceptance' in run(binary, 'info', reopened)
    with zipfile.ZipFile(reopened) as archive:
        assert any(b'Editable content' in archive.read(name) for name in archive.namelist()
                   if name.startswith('ppt/slides/slide') and name.endswith('.xml')), 'Saved shape text lost'
    pdf = out / 'slides.pdf'; run(binary, 'convert', reopened, pdf)
    assert pdf.read_bytes().startswith(b'%PDF-')
    run(binary, 'render', reopened, '--slide', '1', '--scale', '1', out / 'slides.png')
    return {'pptx_edit_save_reopen_pdf_render': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binaries', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--only', choices=('writer', 'sheets', 'pdf', 'slides'))
    args = parser.parse_args()
    if args.output.exists(): parser.error('Use a new fixture output directory')
    args.output.mkdir(parents=True)
    results = {}
    for slug, upstream, test in [('writer', 'wordcraft', writer), ('sheets', 'gridcraft', sheets),
                                  ('pdf', 'pdfcraft', pdf), ('slides', 'deckcraft', slides)]:
        if args.only and args.only != slug: continue
        output = args.output / slug; output.mkdir()
        results[slug] = test(args.binaries / (upstream + '-cli'), output)
        (args.output / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    if not args.only:
        pdf_reader = args.binaries / 'pdfcraft-cli'
        for application in ('writer', 'slides'):
            exported = args.output / application / (application + '.pdf')
            assert 'GREYWARD acceptance' in run(pdf_reader, 'text', exported), 'Exported PDF content lost: ' + application
        results['cross_application_pdf_readback'] = True
        (args.output / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps(results, indent=2))
