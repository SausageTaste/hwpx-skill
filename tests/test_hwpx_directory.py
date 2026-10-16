"""Unit tests for directory editor validation and filesystem safeguards."""
import argparse
import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from hwpx_archive import MIMETYPE
from hwpx_directory import DirectoryEditor, read_directory

class DirectoryEditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'package'
        self.directory.mkdir()
        (self.directory / 'mimetype').write_bytes(MIMETYPE)
        (self.directory / 'header.xml').write_bytes(b'<header/>')
        (self.directory / 'keep.bin').write_bytes(bytes(range(256)))
        self.args = argparse.Namespace(package=self.directory, report=self.root / 'report.json', check=False)

    def test_check_leaves_package_untouched(self):
        self.args.check = True
        editor = DirectoryEditor(self.args)
        proposed = {**editor.original, 'header.xml': b'<header changed="1"/>'}
        editor.finish(proposed, {})
        self.assertEqual(read_directory(self.directory), editor.original)
        report = json.loads(self.args.report.read_text())
        self.assertTrue(report['check_only'])
        self.assertFalse(report['applied'])
        self.assertEqual(report['changed_files'], ['header.xml'])

    def test_apply_writes_only_changed_files(self):
        editor = DirectoryEditor(self.args)
        keep = self.directory / 'keep.bin'
        before = keep.stat().st_mtime_ns
        proposed = {**editor.original, 'header.xml': b'<header changed="1"/>', 'new.bin': b'new'}
        editor.finish(proposed, {})
        self.assertEqual(read_directory(self.directory), proposed)
        self.assertEqual(keep.stat().st_mtime_ns, before)
        editor = DirectoryEditor(self.args)
        stamp = (self.directory / 'header.xml').stat().st_mtime_ns
        editor.finish(proposed, {})
        self.assertEqual((self.directory / 'header.xml').stat().st_mtime_ns, stamp)
        self.assertEqual(json.loads(self.args.report.read_text())['changed_files'], [])

    def test_invalid_xml_never_changes_package(self):
        editor = DirectoryEditor(self.args)
        with self.assertRaises(ET.ParseError):
            editor.finish({**editor.original, 'header.xml': b'<invalid'}, {})
        self.assertEqual(read_directory(self.directory), editor.original)
        self.assertFalse(self.args.report.exists())

    def test_concurrent_changes_are_not_overwritten(self):
        editor = DirectoryEditor(self.args)
        (self.directory / 'header.xml').write_bytes(b'<user-edit/>')
        with self.assertRaises(RuntimeError):
            editor.finish({**editor.original, 'header.xml': b'<agent-edit/>'}, {})
        self.assertEqual((self.directory / 'header.xml').read_bytes(), b'<user-edit/>')

    def test_report_and_traversal_cannot_enter_package(self):
        self.args.report = self.directory / 'report.json'
        with self.assertRaises(ValueError):
            DirectoryEditor(self.args)
        self.args.report = self.root / 'report.json'
        editor = DirectoryEditor(self.args)
        with self.assertRaises(ValueError):
            editor.finish({**editor.original, '../escaped': b'bad'}, {})
        self.assertFalse((self.root / 'escaped').exists())
        with self.assertRaises(ValueError):
            editor.finish({'mimetype': MIMETYPE}, {})
        self.assertEqual(read_directory(self.directory), editor.original)

    def test_failed_write_restores_already_changed_files(self):
        editor = DirectoryEditor(self.args)
        proposed = {**editor.original, 'header.xml': b'<changed/>', 'new.bin': b'new'}
        replace = os.replace
        calls = 0

        def fail_once(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('Simulated write failure')
            return replace(source, destination)

        with patch('hwpx_directory.os.replace', side_effect=fail_once):
            with self.assertRaises(OSError):
                editor.finish(proposed, {})
        self.assertEqual(read_directory(self.directory), editor.original)
        self.assertFalse(self.args.report.exists())


if __name__ == '__main__':
    unittest.main()
