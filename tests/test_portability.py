"""Exercise a relocated skill from a separate working directory."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class PortabilityTests(unittest.TestCase):
    def test_relocated_archive_formatting_workflow(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill = root / 'relocated-skill'
            shutil.copytree(Path(__file__).resolve().parents[1], skill,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            work = root / 'unrelated-workspace'
            work.mkdir()
            env = os.environ.copy()
            env.pop('PYTHONPATH', None)

            def run(script, *args, expected=0):
                result = subprocess.run([sys.executable, '-B',
                                         str(skill / 'scripts' / script), *args],
                                        cwd=work, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)

            run('export_pdf.py', '--help')
            run('export_pdf_job.py', '--help')

            package = work / 'source'
            package.mkdir()
            (package / 'mimetype').write_bytes(b'application/hwp+zip')
            source = (b'<hp:p xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">'
                      b'<hp:run><hp:t>  Example  text\n next </hp:t></hp:run></hp:p>')
            (package / 'section.xml').write_bytes(source)
            (package / 'asset.bin').write_bytes(bytes(range(256)))
            run('hwpx_archive.py', 'pack', 'source', 'before.hwpx')
            run('hwpx_archive.py', 'unpack', 'before.hwpx', 'extracted')
            run('format_hwpx_xml.py', '--report', 'missing-path.json', expected=2)
            run('format_hwpx_xml.py', 'extracted', '--check', '--report', 'check.json')
            self.assertEqual((work / 'extracted/section.xml').read_bytes(), source)
            run('format_hwpx_xml.py', 'extracted', '--report', 'apply.json')
            result = (work / 'extracted/section.xml').read_bytes()
            self.assertNotEqual(result, source)
            self.assertIn(b'<hp:t>  Example  text\n next </hp:t>', result)
            run('format_hwpx_xml.py', 'extracted', '--check', '--report', 'again.json')
            self.assertEqual(json.loads((work / 'again.json').read_text())['changed_files'], [])
            run('hwpx_archive.py', 'pack', 'extracted', 'after.hwpx')
            run('hwpx_archive.py', 'unpack', 'after.hwpx', 'round-trip')
            self.assertEqual((work / 'round-trip/section.xml').read_bytes(), result)
            self.assertEqual((work / 'round-trip/asset.bin').read_bytes(), bytes(range(256)))
            # Optional verification also runs using only the relocated bundle.
            if importlib.util.find_spec('pymupdf'):
                import pymupdf
                with pymupdf.open() as document:
                    page = document.new_page(width=180, height=140)
                    page.insert_text((20, 40), 'Portable PDF')
                    document.save(work / 'example.pdf')
                run('compare_pdf.py', 'example.pdf', 'example.pdf', '--report', 'pdf.json')
                self.assertTrue(json.loads((work / 'pdf.json').read_text())['visually_equal'])


if __name__ == '__main__':
    unittest.main()
