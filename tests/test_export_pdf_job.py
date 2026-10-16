"""Detached job lifecycle tests use synthetic workers, never desktop Hancom."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from export_pdf_job import read_status, run_job, start_job, write_status
from export_pdf import ExportError


class ExportJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'input.hwpx'
        with ZipFile(self.source, 'w') as archive:
            archive.writestr('mimetype', b'application/hwp+zip')
        self.output = self.root / 'output.pdf'
        self.job = self.root / 'job'

    def prepare(self):
        self.job.mkdir()
        config = {'source': str(self.source), 'output': str(self.output),
                  'timeout_seconds': 86400, 'security_module': None}
        (self.job / 'job.json').write_text(json.dumps(config), encoding='utf-8')

    def test_completion_is_persistent_and_same_job_cannot_run_twice(self):
        self.prepare()
        with patch('export_pdf_job.export_pdf', return_value={'source_unchanged': True}) as export:
            self.assertEqual(run_job(self.job), 0)
            with self.assertRaises(FileExistsError):
                run_job(self.job)
        export.assert_called_once_with(str(self.source), str(self.output), 86400, None)
        result = read_status(self.job)
        self.assertEqual(result['state'], 'succeeded')
        self.assertTrue(result['result']['source_unchanged'])
        self.assertIn('finished_utc', result)
        self.assertEqual(list(self.job.glob('*.tmp')), [])

    def test_failure_and_log_remain_inspectable(self):
        self.prepare()
        with patch('export_pdf_job.export_pdf', side_effect=RuntimeError('Export timed out')):
            self.assertEqual(run_job(self.job), 2)
        (self.job / 'progress.log').write_text('Opening document\n', encoding='utf-8')
        result = read_status(self.job)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['error'], 'Export timed out')
        self.assertEqual(result['recent_progress'], ['Opening document'])
        self.assertFalse(self.output.exists())

    def test_timeout_preserves_export_diagnostics(self):
        self.prepare()
        report = {'state': 'timed_out', 'error': 'deadline exceeded',
                  'last_stage': 'Creating staging directory', 'account': 'test'}
        with patch('export_pdf_job.export_pdf', side_effect=ExportError(report)):
            self.assertEqual(run_job(self.job), 2)
        self.assertEqual(read_status(self.job)['result'], report)

    def test_invalid_options_and_collisions_do_not_launch(self):
        with patch('export_pdf_job.sys.platform', 'win32'), patch('export_pdf_job.subprocess.Popen') as launch:
            for timeout, wait in ((0, 30), (float('inf'), 30), (300, -1), (300, 61)):
                with self.assertRaises(ValueError):
                    start_job(self.source, self.output, self.job, timeout, wait)
            self.job.mkdir()
            with self.assertRaises(FileExistsError):
                start_job(self.source, self.output, self.job)
            with self.assertRaises(ValueError):
                start_job(self.source, self.job / 'output.pdf', self.job)
            self.output.write_bytes(b'keep')
            with self.assertRaises(ValueError):
                start_job(self.source, self.output, self.root / 'other')
            self.assertEqual(self.output.read_bytes(), b'keep')
            launch.assert_not_called()

    @unittest.skipUnless(sys.platform == 'win32', 'Windows process detachment')
    def test_launch_failure_is_recorded_without_retry(self):
        with patch('export_pdf_job.subprocess.Popen', side_effect=OSError('Cannot detach')) as launch:
            with self.assertRaises(OSError):
                start_job(self.source, self.output, self.job)
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(read_status(self.job)['state'], 'failed')

    @unittest.skipUnless(sys.platform == 'win32', 'Windows process detachment')
    def test_detached_worker_survives_launcher_exit_and_later_approval(self):
        # Relocate the real job supervisor, substituting only the Hancom backend.
        scripts = self.root / 'scripts'
        scripts.mkdir()
        shutil.copy2(Path(__file__).resolve().parents[1] / 'scripts/export_pdf_job.py', scripts)
        (scripts / 'export_pdf.py').write_text('''from pathlib import Path
import time
def validate_paths(source, output):
    return Path(source).resolve(), Path(output).resolve()
def export_pdf(source, output, timeout, security_module):
    started = time.monotonic()
    while not Path(source).with_suffix('.approve').exists():
        if time.monotonic() - started > timeout:
            raise RuntimeError('Synthetic approval timeout')
        time.sleep(0.02)
    Path(output).write_bytes(b'%PDF-1.4\\n%%EOF\\n')
    return {'source_unchanged': True}
''', encoding='utf-8')
        command = [sys.executable, '-B', str(scripts / 'export_pdf_job.py'), 'start',
                   str(self.source), str(self.output), str(self.job), '--wait', '0', '--timeout', '5']
        launcher = subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(launcher.returncode, 0, launcher.stderr)
        self.assertIn(json.loads(launcher.stdout)['state'], ('queued', 'running'))
        self.assertFalse(self.output.exists())
        # The launcher is gone. User approval arrives later through another file.
        self.source.with_suffix('.approve').touch()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status = read_status(self.job)
            if status['state'] in ('succeeded', 'failed'):
                break
            time.sleep(0.05)
        self.assertEqual(status['state'], 'succeeded', status)
        self.assertTrue(self.output.read_bytes().startswith(b'%PDF-'))
        # Terminal status is written just before Python closes its log handle.
        for attempt in range(100):
            try:
                (self.job / 'progress.log').unlink()
                break
            except PermissionError:
                time.sleep(0.02)
        else:
            self.fail('Detached worker did not release its log after completion')


if __name__ == '__main__':
    unittest.main()
