#!/usr/bin/env python3
"""Export HWPX to PDF locally with installed Hancom on Windows.

Requires pywin32. Existing outputs are refused. A supervised worker bounds COM
calls; approval dialogs are not suppressed and security settings are not edited.
"""
import argparse
import hashlib
import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile

from hwpx_archive import MIMETYPE


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def validate_paths(source, output):
    source, output = Path(source).resolve(strict=True), Path(output).absolute()
    if not source.is_file() or source.suffix.lower() != '.hwpx':
        raise ValueError('Input must be an existing .hwpx file')
    if output.suffix.lower() != '.pdf':
        raise ValueError('Output must have a .pdf extension')
    if output.exists() or output.is_symlink():
        raise ValueError('Output already exists; choose a new path')
    with ZipFile(source) as archive:
        if archive.read('mimetype') != MIMETYPE:
            raise ValueError('Input is not an HWPX package')
        if archive.testzip() is not None:
            raise ValueError('Input ZIP integrity check failed')
    return source, output


def render_with_hancom(source, output, security_module=None):
    """Worker-only COM operation; never attach to an existing active object."""
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    hwp = None
    try:
        print('Starting Hancom COM session', file=sys.stderr, flush=True)
        hwp = win32com.client.DispatchEx('HWPFrame.HwpObject')
        # Keep native approval/error dialogs visible to the desktop owner.
        hwp.XHwpWindows.Item(0).Visible = True
        if security_module and not hwp.RegisterModule('FilePathCheckDLL', security_module):
            raise RuntimeError('Hancom did not accept the named security module')
        version = str(hwp.Version)
        print('Opening HWPX document', file=sys.stderr, flush=True)
        if not hwp.Open(str(source), '', ''):
            raise RuntimeError('Hancom could not open the HWPX document')
        print('Exporting PDF', file=sys.stderr, flush=True)
        if not hwp.SaveAs(str(output), 'PDF', ''):
            raise RuntimeError('Hancom PDF export returned failure')
        return {'renderer': 'Hancom', 'renderer_version': version}
    finally:
        try:
            if hwp is not None:
                print('Closing Hancom COM session', file=sys.stderr, flush=True)
                hwp.Quit()
        finally:
            pythoncom.CoUninitialize()


def run_worker(source, output, timeout, security_module):
    command = [sys.executable, '-B', str(Path(__file__).resolve()),
               str(source), str(output), '--_worker']
    if security_module:
        command.extend(['--security-module', security_module])
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding='utf-8', env=env, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        progress = error.stderr or ''
        if isinstance(progress, bytes):
            progress = progress.decode('utf-8', errors='replace')
        last_stage = progress.strip().splitlines()[-1] if progress.strip() else 'No worker progress received'
        raise RuntimeError(
            'Hancom export timed out; the Python worker was stopped. Hancom may '
            'remain open. Check for a file-access or error dialog before retrying; '
            f'other Hancom processes were not terminated. Last stage: {last_stage}') from error
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f'Hancom export failed: {detail}')
    return json.loads(result.stdout)


def export_pdf(source, output, timeout=120, security_module=None):
    source, output = validate_paths(source, output)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Timeout must be a positive finite number of seconds')
    if sys.platform != 'win32':
        raise RuntimeError('Hancom PDF export requires Windows and desktop Hancom')
    if importlib.util.find_spec('win32com') is None:
        raise RuntimeError('Install pywin32 in this Python environment: python -m pip install pywin32')
    before_hash = file_hash(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Use a sibling staging path so failure never publishes a partial final PDF.
    # A timed-out native renderer may still hold a staging file open on Windows.
    with tempfile.TemporaryDirectory(prefix='.hwpx-pdf-', dir=output.parent,
                                     ignore_cleanup_errors=True) as temporary:
        staged = Path(temporary) / 'export.pdf'
        renderer = run_worker(source, staged, timeout, security_module)
        with staged.open('rb') as stream:
            if not stream.read(8).startswith(b'%PDF-'):
                raise RuntimeError('Hancom output has no PDF signature')
            stream.seek(max(0, staged.stat().st_size - 1024))
            if b'%%EOF' not in stream.read():
                raise RuntimeError('Hancom output has no PDF end marker')
        if file_hash(source) != before_hash:
            raise RuntimeError('Input changed during export; PDF was not published')
        # Exclusive creation protects an output that appeared while exporting.
        with output.open('xb') as target:
            try:
                with staged.open('rb') as incoming:
                    shutil.copyfileobj(incoming, target)
            except BaseException:
                target.close()
                output.unlink()
                raise
    return {**renderer, 'source': str(source), 'output': str(output),
            'source_sha256': before_hash, 'output_sha256': file_hash(output),
            'output_bytes': output.stat().st_size, 'source_unchanged': True,
            'pdf_signature_verified': True, 'visual_equivalence_verified': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path, help='New .pdf output path')
    parser.add_argument('--timeout', type=float, default=120, help='Export timeout in seconds (default: 120)')
    parser.add_argument('--security-module', help='Name of an already installed Hancom approval module')
    parser.add_argument('--_worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args._worker:
            source, output = validate_paths(args.source, args.output)
            result = render_with_hancom(source, output, args.security_module)
        else:
            result = export_pdf(args.source, args.output, args.timeout, args.security_module)
    except Exception as error:
        parser.exit(2, f'Error: {error}\n')
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
