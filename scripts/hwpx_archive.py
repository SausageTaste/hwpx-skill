#!/usr/bin/env python3
"""Unpack HWPX into a directory or pack a directory without editing its contents.

Usage:
    python scripts/hwpx_archive.py unpack document.hwpx unpacked
    python scripts/hwpx_archive.py pack unpacked rebuilt.hwpx

Requires Python 3.10+; no third-party packages. Existing outputs are refused.
"""
import argparse
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

MIMETYPE = b'application/hwp+zip'


def safe_name(name):
    """Reject ambiguous or unsafe names, including Windows aliases and streams."""
    parts = name.rstrip('/').split('/')
    reserved = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', re.I)
    if not name or any(
        part in ('', '.', '..') or part.endswith((' ', '.'))
        or reserved.match(part) or re.search(r'[\\:<>"|?*\x00-\x1f]', part)
        for part in parts
    ):
        raise ValueError(f'Unsafe package path: {name!r}')
    return parts


def require_new_output(path):
    if path.exists() or path.is_symlink():
        raise ValueError(f'Output already exists; choose another path: {path}')


def is_link(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def unpack(source, destination):
    require_new_output(destination)
    with ZipFile(source) as archive:
        entries = archive.infolist()
        seen = set()
        for info in entries:
            # On Windows, ZipInfo normalizes backslashes while reading. Validate
            # the original spelling as well, including any embedded NUL byte.
            safe_name(info.orig_filename)
            if info.orig_filename != info.filename:
                raise ValueError(f'Normalized package path is unsupported: {info.orig_filename!r}')
            key = info.filename.rstrip('/').casefold()
            if key in seen:
                raise ValueError(f'Duplicate package path: {info.filename}')
            seen.add(key)
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError(f'Symbolic link in package: {info.filename}')
            if info.flag_bits & 1:
                raise ValueError(f'Encrypted ZIP entry is unsupported: {info.filename}')
        if 'mimetype' not in archive.namelist() or archive.read('mimetype') != MIMETYPE:
            raise ValueError('Not an HWPX package: mimetype must be application/hwp+zip.')
        bad = archive.testzip()
        if bad:
            raise ValueError(f'ZIP integrity check failed: {bad}')
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Stage beside the destination: failed extraction never leaves a partial tree.
        with tempfile.TemporaryDirectory(prefix='.hwpx-unpack-', dir=destination.parent) as temp:
            staging = Path(temp) / 'contents'
            staging.mkdir()
            for info in entries:
                target = staging.joinpath(*safe_name(info.filename))
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as incoming, target.open('xb') as outgoing:
                        shutil.copyfileobj(incoming, outgoing)
            require_new_output(destination)
            staging.rename(destination)
    return sum(not i.is_dir() for i in entries)


def pack(source, destination):
    require_new_output(destination)
    if is_link(source) or not source.is_dir():
        raise ValueError(f'Source must be a regular directory: {source}')
    if destination.resolve().is_relative_to(source.resolve()):
        raise ValueError('Output HWPX must be outside the source directory.')
    files, directories, seen = {}, [], set()
    for current, dirs, names in os.walk(source, followlinks=False):
        for name in dirs + names:
            path = Path(current) / name
            relative = path.relative_to(source).as_posix()
            safe_name(relative)
            if is_link(path):
                raise ValueError(f'Symbolic links and junctions are unsupported: {path}')
            if relative.casefold() in seen:
                raise ValueError(f'Case-insensitive path collision: {relative}')
            seen.add(relative.casefold())
            if path.is_dir():
                # Preserve empty directories, but avoid redundant parent entries.
                if not any(path.iterdir()):
                    directories.append(relative + '/')
            elif path.is_file():
                files[relative] = path
            else:
                raise ValueError(f'Unsupported filesystem entry: {path}')
    if 'mimetype' not in files or files['mimetype'].read_bytes() != MIMETYPE:
        raise ValueError('Source must contain an unchanged HWPX mimetype file.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Fixed metadata and order make repeated builds stable on the same Python/zlib.
    with tempfile.TemporaryFile() as temporary:
        with ZipFile(temporary, 'w') as archive:
            for name in ['mimetype'] + sorted((files.keys() - {'mimetype'}) | set(directories)):
                info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.compress_type = ZIP_STORED if name in ('mimetype', 'version.xml') else ZIP_DEFLATED
                info.external_attr = ((stat.S_IFDIR | 0o755) << 16 | 0x10) if name.endswith('/') else ((stat.S_IFREG | 0o644) << 16)
                if name.endswith('/'):
                    archive.writestr(info, b'')
                else:
                    info.file_size = files[name].stat().st_size
                    with files[name].open('rb') as incoming, archive.open(info, 'w') as outgoing:
                        shutil.copyfileobj(incoming, outgoing)
        temporary.seek(0)
        with ZipFile(temporary) as archive:
            bad = archive.testzip()
            if bad:
                raise ValueError(f'ZIP integrity check failed: {bad}')
        temporary.seek(0)
        with destination.open('xb') as outgoing:
            try:
                shutil.copyfileobj(temporary, outgoing)
            except BaseException:
                outgoing.close()
                destination.unlink()
                raise
    return len(files)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    for command, help_text in [('unpack', 'Extract HWPX into a new directory'),
                               ('pack', 'Build HWPX from a directory')]:
        sub = commands.add_parser(command, help=help_text)
        sub.add_argument('source', type=Path)
        sub.add_argument('output', type=Path, help='New output path; existing paths are never overwritten')
    args = parser.parse_args()
    try:
        count = (unpack if args.command == 'unpack' else pack)(args.source, args.output)
    except (OSError, ValueError, BadZipFile, RuntimeError, NotImplementedError) as error:
        parser.exit(1, f'Error: {error}\n')
    print(f'{args.command}: {count} files -> {args.output}')


if __name__ == '__main__':
    main()
