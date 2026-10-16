"""Run the real pipeline in supervised subprocesses with synthetic Hancom only."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import export_pdf
from hwpx_archive import MIMETYPE


class ExportDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / '교범 원본.hwpx'
        self.output = self.root / '검증 결과.pdf'
        self.report = self.root / '검증 보고서.json'
        with ZipFile(self.source, 'w') as archive:
            archive.writestr('mimetype', MIMETYPE)
        self.original = self.source.read_bytes()
        self.wrapper = self.root / 'synthetic_backend.py'

    def backend(self, mode):
        scripts = str(Path(export_pdf.__file__).resolve().parent)
        self.wrapper.write_text(f'''
import sys, time
from pathlib import Path
sys.path.insert(0, {scripts!r})
import export_pdf as module
mode = {mode!r}
module.sys.platform = 'win32'
module.execution_context = lambda: {{'account': 'synthetic-account', 'session_id': 7}}
module.importlib.util.find_spec = lambda name: object()
def render(source, staged, security_module, progress):
    progress('Exporting PDF', renderer='synthetic', renderer_version='1')
    if mode == 'render':
        time.sleep(60)
    staged.write_bytes(b'%PDF-1.4\\nsynthetic\\n%%EOF\\n')
    return {{'renderer': 'synthetic', 'renderer_version': '1'}}
module.render_with_hancom = render
if mode in ('prepare', 'denied', 'cleanup'):
    original = module.tempfile.TemporaryDirectory
    class TemporaryDirectory(original):
        def __init__(self, *args, **kwargs):
            if mode == 'prepare':
                time.sleep(60)
            if mode == 'denied':
                raise PermissionError('staging directory access denied')
            super().__init__(*args, **kwargs)
        def cleanup(self):
            if mode == 'cleanup':
                time.sleep(60)
            super().cleanup()
    module.tempfile.TemporaryDirectory = TemporaryDirectory
if mode == 'validate':
    module.validate_paths = lambda *args: time.sleep(60)
if mode == 'publish':
    module.publish_new = lambda *args: time.sleep(60)
if mode == 'report':
    module.write_report = lambda *args: time.sleep(60)
raise SystemExit(module.main())
''', encoding='utf-8')

    def run_export(self, mode='success', timeout=10):
        self.backend(mode)
        with patch('export_pdf.__file__', str(self.wrapper)), \
                patch('export_pdf.sys.stderr', new_callable=io.StringIO):
            return export_pdf.export_pdf(self.source, self.output, timeout, report_path=self.report)

    def test_deadline_covers_validation_preparation_rendering_publication_and_cleanup(self):
        stages = {'validate': 'Validating paths and HWPX archive',
                  'prepare': 'Creating staging directory', 'render': 'Exporting PDF',
                  'publish': 'Publishing PDF', 'cleanup': 'Cleaning staging directory'}
        for mode, stage in stages.items():
            with self.subTest(mode=mode):
                started = time.monotonic()
                with self.assertRaises(export_pdf.ExportError) as failure:
                    self.run_export(mode, timeout=1)
                result = failure.exception.report
                self.assertLess(time.monotonic() - started, 5)
                self.assertEqual(result['state'], 'timed_out')
                self.assertEqual(result['last_stage'], stage)
                self.assertEqual(result['account'], 'synthetic-account')
                self.assertEqual(result['session_id'], 7)
                self.assertEqual(self.source.read_bytes(), self.original)
                if mode == 'cleanup':
                    self.assertTrue(result['output_published'])
                    self.assertTrue(self.output.read_bytes().endswith(b'%%EOF\n'))
                    self.output.unlink()
                else:
                    self.assertFalse(self.output.exists())
                # Persist timeout diagnostics using the real bounded report writer.
                export_pdf.save_report(self.report, result)
                saved = json.loads(self.report.read_text(encoding='utf-8'))
                self.assertEqual(saved['last_stage'], stage)
                self.report.unlink()

    def test_unwritable_staging_fails_before_hancom_and_preserves_input(self):
        with self.assertRaises(export_pdf.ExportError) as failure:
            self.run_export('denied')
        result = failure.exception.report
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['failed_stage'], 'Creating staging directory')
        self.assertIn('access denied', result['error'])
        self.assertNotIn('renderer', result)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_korean_paths_and_success_report_round_trip_without_overwrite(self):
        result = self.run_export()
        export_pdf.save_report(self.report, result)
        saved = json.loads(self.report.read_text(encoding='utf-8'))
        self.assertEqual(saved['source'], str(self.source))
        self.assertEqual(saved['output'], str(self.output))
        self.assertEqual(saved['state'], 'succeeded')
        self.assertTrue(saved['source_unchanged'])
        self.assertEqual(saved['source_sha256'], export_pdf.file_hash(self.source))
        self.assertEqual(saved['output_sha256'], export_pdf.file_hash(self.output))
        self.assertGreater(saved['elapsed_seconds'], 0)
        original_report = self.report.read_bytes()
        with self.assertRaises(subprocess.CalledProcessError):
            export_pdf.save_report(self.report, {**result, 'error': 'replacement'})
        self.assertEqual(self.report.read_bytes(), original_report)

    def test_report_collisions_are_rejected_before_rendering(self):
        for target in (self.source, self.output):
            with self.subTest(target=target):
                self.report = target
                with self.assertRaises(export_pdf.ExportError) as failure:
                    self.run_export()
                self.assertIn('separate', str(failure.exception))
                self.assertNotIn('renderer', failure.exception.report)
                self.assertFalse(self.output.exists())
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_report_writer_has_its_own_deadline(self):
        self.backend('report')
        started = time.monotonic()
        with patch('export_pdf.__file__', str(self.wrapper)), \
                patch('export_pdf.REPORT_TIMEOUT', 0.5):
            with self.assertRaises(subprocess.TimeoutExpired):
                export_pdf.save_report(self.report, export_pdf.new_report(self.source, self.output))
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(self.report.exists())

    def test_cli_saves_failure_as_json_with_nonzero_exit(self):
        with patch('export_pdf.sys.stdout', new_callable=io.StringIO) as output, \
                patch('export_pdf.sys.stderr', new_callable=io.StringIO):
            code = export_pdf.main([str(self.root / 'missing.hwpx'), str(self.output),
                                    '--report', str(self.report)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue()), json.loads(self.report.read_text()))
        self.assertEqual(json.loads(output.getvalue())['state'], 'failed')

    def test_report_error_keeps_success_result_on_stdout(self):
        result = {**export_pdf.new_report(self.source, self.output), 'state': 'succeeded'}
        with patch('export_pdf.export_pdf', return_value=result), \
                patch('export_pdf.save_report', side_effect=PermissionError('report access denied')), \
                patch('export_pdf.sys.stdout', new_callable=io.StringIO) as output, \
                patch('export_pdf.sys.stderr', new_callable=io.StringIO):
            code = export_pdf.main([str(self.source), str(self.output), '--report', str(self.report)])
        self.assertEqual(code, 2)
        saved = json.loads(output.getvalue())
        self.assertEqual(saved['state'], 'succeeded')
        self.assertIn('access denied', saved['report_error'])

    def test_invalid_timeout_still_saves_failure_report(self):
        with patch('export_pdf.sys.stdout', new_callable=io.StringIO), \
                patch('export_pdf.sys.stderr', new_callable=io.StringIO):
            code = export_pdf.main([str(self.source), str(self.output), '--timeout', '0',
                                    '--report', str(self.report)])
        self.assertEqual(code, 2)
        saved = json.loads(self.report.read_text())
        self.assertEqual(saved['state'], 'failed')
        self.assertIn('elapsed_seconds', saved)
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
