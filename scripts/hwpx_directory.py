"""Shared directory I/O for HWPX editors; no ZIP creation or extraction."""
import argparse
import hashlib
import json
import os
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from hwpx_archive import MIMETYPE, is_link, safe_name


def editor_arguments(description):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('package', type=Path,
                        help='Explicit unpacked package directory, edited in place')
    parser.add_argument('--report', required=True, type=Path,
                        help='Verification JSON outside the package directory')
    parser.add_argument('--check', action='store_true',
                        help='Validate proposed changes and write a report without changing package files')
    return parser.parse_args()


def read_directory(directory):
    if is_link(directory) or not directory.is_dir():
        raise ValueError(f'Expected an unpacked HWPX directory: {directory}')
    entries = {}
    def walk_error(error):
        raise error

    for current, dirs, files in os.walk(directory, onerror=walk_error):
        for name in dirs + files:
            path = Path(current) / name
            relative = path.relative_to(directory).as_posix()
            safe_name(relative)
            if is_link(path):
                raise ValueError(f'Links and junctions are unsupported: {path}')
            if name in files:
                entries[relative] = path.read_bytes()
    if entries.get('mimetype') != MIMETYPE:
        raise ValueError('Missing or invalid HWPX mimetype file.')
    return entries


def package_digest(entries):
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(entries.items())}
    return hashlib.sha256(json.dumps(hashes, ensure_ascii=True, sort_keys=True).encode()).hexdigest()


class DirectoryEditor:
    def __init__(self, args):
        self.directory = args.package.resolve()
        self.report_path = args.report.resolve()
        self.check = args.check
        if self.report_path.is_relative_to(self.directory):
            raise ValueError('The verification report must be outside the package directory.')
        self.original = read_directory(args.package)

    def finish(self, entries, report):
        """Validate first, then replace only changed files, restoring them on failure."""
        if self.original.keys() - entries.keys():
            raise ValueError('Editors may not remove existing package entries.')
        for name, data in entries.items():
            safe_name(name)
            if name.endswith(('.xml', '.hpf', '.rdf')):
                ET.fromstring(data)
        if entries.get('mimetype') != MIMETYPE:
            raise ValueError('Editors may not change the HWPX mimetype.')
        changed = sorted(name for name in entries if entries[name] != self.original.get(name))
        if read_directory(self.directory) != self.original:
            raise RuntimeError('Package changed during validation; no edits were saved.')
        report.update(package_directory=str(self.directory),
                      source_package_sha256=package_digest(self.original),
                      result_package_sha256=package_digest(entries),
                      package_hash_method='SHA-256 of sorted JSON mapping paths to file SHA-256 hashes',
                      changed_files=changed, check_only=self.check,
                      applied=not self.check, all_xml_parse=True)
        # Stage all data and the report before the first package write.
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='hwpx-edit-', dir=self.directory.parent) as temp:
            staging = Path(temp)
            for index, name in enumerate(changed):
                (staging / str(index)).write_bytes(entries[name])
            with tempfile.NamedTemporaryFile(dir=self.report_path.parent, delete=False) as pending:
                pending_path = Path(pending.name)
                pending.write((json.dumps(report, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
            written = []
            try:
                if not self.check:
                    for index, name in enumerate(changed):
                        target = self.directory / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(staging / str(index), target)
                        written.append(name)
                    if read_directory(self.directory) != entries:
                        raise RuntimeError('Saved package differs from the validated result.')
                os.replace(pending_path, self.report_path)
            except BaseException:
                for name in reversed(written):
                    target = self.directory / name
                    if name in self.original:
                        backup = staging / 'restore'
                        backup.write_bytes(self.original[name])
                        os.replace(backup, target)
                    else:
                        target.unlink()
                raise
            finally:
                pending_path.unlink(missing_ok=True)
        print(json.dumps({'package': str(self.directory), 'changed_files': changed,
                          'check_only': self.check, 'report': str(self.report_path)}))
