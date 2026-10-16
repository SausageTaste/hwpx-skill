"""Round-trip and filesystem boundary tests for the HWPX archive CLI."""
import tempfile
import unittest
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from hwpx_archive import MIMETYPE, pack, unpack


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'mimetype').write_bytes(MIMETYPE)
        (self.source / 'Contents').mkdir()
        (self.source / 'Contents/header.xml').write_bytes(b'<?xml version="1.0"?>\r\n<header />\n')
        (self.source / 'image.png').write_bytes(bytes(range(256)))
        (self.source / 'empty').mkdir()

    def test_round_trip_preserves_bytes_and_build_is_repeatable(self):
        first, second = self.root / 'first.hwpx', self.root / 'second.hwpx'
        pack(self.source, first)
        extracted = self.root / 'unpacked'
        unpack(first, extracted)
        for path in self.source.rglob('*'):
            target = extracted / path.relative_to(self.source)
            self.assertEqual(path.is_dir(), target.is_dir())
            if path.is_file():
                self.assertEqual(path.read_bytes(), target.read_bytes())
        pack(extracted, second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with ZipFile(first) as archive:
            self.assertEqual(archive.infolist()[0].filename, 'mimetype')
            self.assertEqual(archive.infolist()[0].compress_type, ZIP_STORED)

    def test_outputs_are_never_overwritten(self):
        target = self.root / 'keep.hwpx'
        target.write_bytes(b'keep')
        with self.assertRaises(ValueError):
            pack(self.source, target)
        self.assertEqual(target.read_bytes(), b'keep')
        with self.assertRaises(ValueError):
            unpack(target, self.source)

    def test_output_cannot_be_inside_source(self):
        with self.assertRaises(ValueError):
            pack(self.source, self.source / 'nested/out.hwpx')
        self.assertFalse((self.source / 'nested').exists())

    def test_unsafe_zip_names_never_extract(self):
        for index, name in enumerate(['../escaped', '/absolute', 'C:/drive', 'file:stream', 'NUL', 'dir\\file', 'dir/../x']):
            with self.subTest(name=name):
                archive = self.root / 'unsafe.hwpx'
                with ZipFile(archive, 'w') as out:
                    out.writestr('mimetype', MIMETYPE)
                    # ZipInfo normally normalizes backslashes on Windows. Keep the
                    # malicious name verbatim to exercise the extraction boundary.
                    info = ZipInfo('placeholder')
                    info.filename = name
                    out.writestr(info, b'untrusted')
                destination = self.root / f'extracted-{index}'
                with self.assertRaises(ValueError):
                    unpack(archive, destination)
                self.assertFalse(destination.exists())
                self.assertFalse((self.root / 'escaped').exists())

    def test_case_collision_and_invalid_mimetype(self):
        archive = self.root / 'duplicate.hwpx'
        with ZipFile(archive, 'w') as out:
            out.writestr('mimetype', MIMETYPE)
            out.writestr('Contents/a.xml', b'a')
            out.writestr('contents/A.xml', b'b')
        with self.assertRaises(ValueError):
            unpack(archive, self.root / 'out')
        (self.source / 'mimetype').write_bytes(b'not hwpx')
        with self.assertRaises(ValueError):
            pack(self.source, self.root / 'invalid.hwpx')
        self.assertFalse((self.root / 'invalid.hwpx').exists())


if __name__ == '__main__':
    unittest.main()
