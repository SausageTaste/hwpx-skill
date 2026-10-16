"""Export orchestration tests never launch a desktop app."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from zipfile import ZipFile

from export_pdf import export_pdf, main, render_with_hancom, run_worker
from hwpx_archive import MIMETYPE

PDF = b'%PDF-1.4\nsynthetic export test\n%%EOF\n'


class PdfExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'input.hwpx'
        with ZipFile(self.source, 'w') as archive:
            archive.writestr('mimetype', MIMETYPE)
            archive.writestr('Contents/section0.xml', b'<section/>')
        self.output = self.root / 'output.pdf'

    def export(self, worker):
        with patch('export_pdf.sys.platform', 'win32'), \
                patch('export_pdf.importlib.util.find_spec', return_value=object()), \
                patch('export_pdf.run_worker', side_effect=worker):
            return export_pdf(self.source, self.output)

    def successful_worker(self, source, staged, timeout, module):
        staged.write_bytes(PDF)
        return {'renderer': 'Hancom', 'renderer_version': 'test'}

    def test_publishes_pdf_and_preserves_input(self):
        original = self.source.read_bytes()
        report = self.export(self.successful_worker)
        self.assertEqual(self.output.read_bytes(), PDF)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertTrue(report['source_unchanged'])
        self.assertTrue(report['pdf_signature_verified'])
        self.assertFalse(report['visual_equivalence_verified'])
        self.assertFalse(list(self.root.glob('.hwpx-pdf-*')))

    def test_refuses_existing_output_invalid_package_and_extension(self):
        self.output.write_bytes(b'keep')
        with self.assertRaises(ValueError):
            export_pdf(self.source, self.output)
        self.assertEqual(self.output.read_bytes(), b'keep')
        with self.assertRaises(ValueError):
            export_pdf(self.source, self.root / 'output.hwpx')
        invalid = self.root / 'invalid.hwpx'
        with ZipFile(invalid, 'w') as archive:
            archive.writestr('mimetype', b'not HWPX')
        with self.assertRaises(ValueError):
            export_pdf(invalid, self.root / 'invalid.pdf')

    def test_rejects_unsupported_platform_and_missing_dependency(self):
        with patch('export_pdf.sys.platform', 'linux'):
            with self.assertRaisesRegex(RuntimeError, 'Windows'):
                export_pdf(self.source, self.output)
        with patch('export_pdf.sys.platform', 'win32'), \
                patch('export_pdf.importlib.util.find_spec', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'pywin32'):
                export_pdf(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_rejects_invalid_pdf_and_worker_failure(self):
        for payload in (b'', b'%PDF-1.4\ntruncated', b'wrong %%EOF'):
            def worker(source, staged, timeout, module):
                staged.write_bytes(payload)
                return {}
            with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                self.export(worker)
            self.assertFalse(self.output.exists())
        with self.assertRaises(RuntimeError):
            self.export(MagicMock(side_effect=RuntimeError('worker failed')))
        self.assertFalse(self.output.exists())

    def test_detects_concurrent_input_or_output_change(self):
        original = self.source.read_bytes()
        def source_changes(*args):
            result = self.successful_worker(*args)
            self.source.write_bytes(b'changed during export')
            return result
        with self.assertRaisesRegex(RuntimeError, 'Input changed'):
            self.export(source_changes)
        self.assertFalse(self.output.exists())
        self.source.write_bytes(original)
        def output_appears(*args):
            result = self.successful_worker(*args)
            self.output.write_bytes(b'user output')
            return result
        with self.assertRaises(FileExistsError):
            self.export(output_appears)
        self.assertEqual(self.output.read_bytes(), b'user output')

    def test_timeout_has_clear_error_and_no_automatic_retry(self):
        with patch('export_pdf.subprocess.run', side_effect=subprocess.TimeoutExpired('worker', 1)) as run:
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                run_worker(self.source, self.output, 1, None)
            self.assertEqual(run.call_count, 1)
        for value in (0, -1, float('nan'), float('inf')):
            with self.subTest(timeout=value), self.assertRaises(ValueError):
                export_pdf(self.source, self.output, timeout=value)

    def test_copy_failure_removes_partial_output(self):
        with patch('export_pdf.shutil.copyfileobj', side_effect=OSError('copy failed')):
            with self.assertRaises(OSError):
                self.export(self.successful_worker)
        self.assertFalse(self.output.exists())

    def test_native_export_uses_local_pdf_api_and_closes_session(self):
        pythoncom, client = MagicMock(), MagicMock()
        parent = MagicMock()
        parent.client = client
        hwp = client.DispatchEx.return_value
        hwp.Version = 'test-version'
        modules = {'pythoncom': pythoncom, 'win32com': parent, 'win32com.client': client}
        with patch.dict('sys.modules', modules):
            report = render_with_hancom(self.source, self.output)
        hwp.Open.assert_called_once_with(str(self.source), '', '')
        hwp.SaveAs.assert_called_once_with(str(self.output), 'PDF', '')
        hwp.Quit.assert_called_once()
        self.assertEqual(report['renderer_version'], 'test-version')
        hwp.reset_mock()
        hwp.SaveAs.return_value = False
        with patch.dict('sys.modules', modules):
            with self.assertRaisesRegex(RuntimeError, 'PDF export returned failure'):
                render_with_hancom(self.source, self.output)
        hwp.Quit.assert_called_once()

    def test_com_session_cleanup_on_failure_and_named_module(self):
        pythoncom = MagicMock()
        client = MagicMock()
        hwp = client.DispatchEx.return_value
        modules = {'pythoncom': pythoncom, 'win32com': MagicMock(), 'win32com.client': client}
        # Use a real module parent so dotted import resolves the mocked client.
        modules['win32com'].client = client
        hwp.Open.return_value = False
        with patch.dict('sys.modules', modules):
            with self.assertRaisesRegex(RuntimeError, 'could not open'):
                render_with_hancom(self.source, self.output)
        client.DispatchEx.assert_called_once_with('HWPFrame.HwpObject')
        hwp.Quit.assert_called_once()
        pythoncom.CoUninitialize.assert_called_once()
        hwp.RegisterModule.assert_not_called()
        hwp.reset_mock()
        hwp.RegisterModule.return_value = False
        with patch.dict('sys.modules', modules):
            with self.assertRaisesRegex(RuntimeError, 'security module'):
                render_with_hancom(self.source, self.output, 'InstalledModule')
        hwp.RegisterModule.assert_called_once_with('FilePathCheckDLL', 'InstalledModule')
        hwp.Quit.assert_called_once()

    def test_cli_reports_failure(self):
        with patch('export_pdf.export_pdf', side_effect=RuntimeError('Hancom unavailable')):
            with self.assertRaises(SystemExit) as result:
                main([str(self.source), str(self.output)])
        self.assertEqual(result.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
