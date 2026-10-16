"""Synthetic PDF comparisons; no source documents or fonts are needed."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from compare_pdf import compare, main


@unittest.skipUnless(importlib.util.find_spec('pymupdf'), 'PyMuPDF is optional')
class PdfComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def pdf(self, name, text='Example text', position=(20, 40), pages=1, width=180):
        import pymupdf
        path = self.root / name
        with pymupdf.open() as document:
            for _ in range(pages):
                page = document.new_page(width=width, height=140)
                page.insert_text(position, text)
            document.save(path)
        return path

    def test_equal_pages_and_text(self):
        a, b = self.pdf('a.pdf'), self.pdf('b.pdf')
        report = compare(a, b)
        self.assertTrue(report['visually_equal'])
        self.assertTrue(report['text_equal'])
        self.assertEqual(report['different_pages'], [])
        self.assertEqual(report['pages'][0]['before']['render_sha256'],
                         report['pages'][0]['after']['render_sha256'])

    def test_text_and_layout_changes(self):
        a = self.pdf('a.pdf')
        text = compare(a, self.pdf('text.pdf', text='Changed text'))
        self.assertEqual(text['text_different_pages'], [1])
        self.assertEqual(text['different_pages'], [1])
        layout = compare(a, self.pdf('layout.pdf', position=(20, 65)))
        self.assertTrue(layout['text_equal'])
        self.assertEqual(layout['different_pages'], [1])

    def test_different_page_counts_and_sizes(self):
        a = self.pdf('a.pdf')
        b = self.pdf('b.pdf', pages=2)
        for first, second in ((a, b), (b, a)):
            result = compare(first, second)
            self.assertEqual(result['different_pages'], [2])
            self.assertEqual(result['text_different_pages'], [2])
        result = compare(a, self.pdf('wide.pdf', width=200))
        self.assertFalse(result['pages'][0]['dimensions_equal'])
        self.assertEqual(result['different_pages'], [1])

    def test_cli_exit_codes_and_report_protection(self):
        a, b = self.pdf('a.pdf'), self.pdf('b.pdf', text='Changed')
        report = self.root / 'report.json'
        self.assertEqual(main([str(a), str(a), '--report', str(report)]), 0)
        original = report.read_bytes()
        with self.assertRaises(SystemExit) as error:
            main([str(a), str(b), '--report', str(report)])
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(report.read_bytes(), original)
        with self.assertRaises(SystemExit):
            main([str(a), str(b), '--report', str(a)])
        changed = self.root / 'changed.json'
        self.assertEqual(main([str(a), str(b), '--report', str(changed)]), 1)
        self.assertEqual(json.loads(changed.read_text())['different_pages'], [1])
        invalid = self.root / 'invalid.pdf'
        invalid.write_bytes(b'not a PDF')
        with self.assertRaises(SystemExit) as error:
            main([str(invalid), str(a), '--report', str(self.root / 'invalid.json')])
        self.assertEqual(error.exception.code, 2)
        self.assertFalse((self.root / 'invalid.json').exists())


if __name__ == '__main__':
    unittest.main()
