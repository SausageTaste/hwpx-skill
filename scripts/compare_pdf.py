#!/usr/bin/env python3
"""Compare PDF exports using exact 300 DPI RGB rendering and extracted text.

Requires PyMuPDF only for this operation. Exit 0 means all pages and text match,
1 means differences were recorded, and 2 means an execution error.
"""
import argparse
import hashlib
import json
from pathlib import Path


def compare(before, after):
    try:
        import pymupdf as fitz
    except ImportError as error:
        raise RuntimeError('PDF comparison requires PyMuPDF: python -m pip install pymupdf') from error

    before, after = Path(before), Path(after)
    # Snapshot bytes keep fingerprints tied to the exact rendered input and
    # avoid native file handles remaining locked after a failed PDF open.
    payloads = [path.read_bytes() for path in (before, after)]
    inputs = [{'path': str(path.resolve()), 'bytes': len(data),
               'sha256': hashlib.sha256(data).hexdigest()}
              for path, data in zip((before, after), payloads)]
    with fitz.open(stream=payloads[0], filetype='pdf') as a, \
            fitz.open(stream=payloads[1], filetype='pdf') as b:
        if not a.is_pdf or not b.is_pdf:
            raise ValueError('Both inputs must be PDF documents')
        if a.needs_pass or b.needs_pass:
            raise ValueError('Password-protected PDFs are unsupported')
        counts = [len(a), len(b)]
        if not all(counts):
            raise ValueError('PDF inputs must contain at least one page')
        report = {'renderer': 'PyMuPDF ' + fitz.VersionBind, 'dpi': 300,
                  'color': 'RGB, opaque', 'pixel_tolerance': 0,
                  'page_numbering': '1-based PDF page index', 'inputs': inputs,
                  'page_counts': counts, 'pages': []}
        for index in range(max(counts)):
            row = {'page': index + 1}
            renders, rectangles, texts = [], [], []
            for label, document in (('before', a), ('after', b)):
                if index >= len(document):
                    row[label] = None
                    continue
                page = document[index]
                pixmap = page.get_pixmap(dpi=300, colorspace=fitz.csRGB, alpha=False)
                samples = pixmap.samples
                rect = list(page.rect)
                text = page.get_text()
                row[label] = {'page_rect_points': rect,
                              'pixel_size': [pixmap.width, pixmap.height],
                              'rotation': page.rotation,
                              'render_sha256': hashlib.sha256(samples).hexdigest(),
                              'text_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}
                renders.append(samples)
                rectangles.append(rect)
                texts.append(text)
            row['dimensions_equal'] = len(rectangles) == 2 and rectangles[0] == rectangles[1]
            row['pixels_equal'] = (len(renders) == 2
                                   and row['before']['pixel_size'] == row['after']['pixel_size']
                                   and renders[0] == renders[1])
            row['text_equal'] = len(texts) == 2 and texts[0] == texts[1]
            report['pages'].append(row)
    report['different_pages'] = [r['page'] for r in report['pages']
                                 if not (r['dimensions_equal'] and r['pixels_equal'])]
    report['text_different_pages'] = [r['page'] for r in report['pages'] if not r['text_equal']]
    report['visually_equal'] = not report['different_pages']
    report['text_equal'] = not report['text_different_pages']
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('before', type=Path)
    parser.add_argument('after', type=Path)
    parser.add_argument('--report', required=True, type=Path, help='New JSON report path')
    args = parser.parse_args(argv)
    try:
        target = args.report.resolve()
        if target in (args.before.resolve(), args.after.resolve()):
            raise ValueError('Report must not replace either input PDF')
        if args.report.exists() or args.report.is_symlink():
            raise ValueError('Report already exists; choose a new path')
        report = compare(args.before, args.after)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open('x', encoding='utf-8') as output:
            json.dump(report, output, indent=2)
            output.write('\n')
    except Exception as error:
        parser.exit(2, f'Error: {error}\n')
    print(json.dumps({'report': str(args.report), 'visually_equal': report['visually_equal'],
                      'different_pages': report['different_pages'],
                      'text_different_pages': report['text_different_pages']}))
    return 0 if report['visually_equal'] and report['text_equal'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
