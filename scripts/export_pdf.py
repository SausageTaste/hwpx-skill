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
import threading
import time
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
    started = time.monotonic()
    progress = []

    def forward(stream):
        # communicate(timeout=...) buffers stderr until EOF on Windows.
        # Read lines independently so stages remain visible during COM calls.
        for line in stream:
            progress.append(line)
            print(line, end='', file=sys.stderr, flush=True)

    with tempfile.TemporaryFile() as result_file, \
            subprocess.Popen(command, stdout=result_file, stderr=subprocess.PIPE,
                             text=True, encoding='utf-8', env=env) as process:
        reader = threading.Thread(target=forward, args=(process.stderr,), daemon=True)
        reader.start()
        try:
            while True:
                remaining = timeout - (time.monotonic() - started)
                try:
                    process.wait(timeout=max(0, min(15, remaining)))
                    break
                except subprocess.TimeoutExpired as error:
                    elapsed = time.monotonic() - started
                    size = output.stat().st_size if output.exists() else 0
                    print(f'Hancom export running: {elapsed:.0f}s elapsed; '
                          f'staged PDF {size:,} bytes', file=sys.stderr, flush=True)
                    if elapsed >= timeout:
                        process.kill()
                        process.wait()
                        reader.join()
                        last_stage = (progress[-1].strip() if progress
                                      else 'No worker progress received')
                        raise RuntimeError(
                            f'Hancom exceeded the {timeout:g}s time limit; this does not '
                            'necessarily mean conversion stalled. The Python worker was '
                            'stopped, but Hancom may still be rendering. Inspect its state '
                            'before retrying with a larger --timeout. Other Hancom '
                            f'processes were not terminated. Last stage: {last_stage}. '
                            f'Staging path: {output}') from error
        except BaseException:
            # Also bound cleanup on interruption or an unexpected monitoring error.
            if process.poll() is None:
                process.kill()
                process.wait()
            raise
        finally:
            reader.join()
        result_file.seek(0)
        stdout = result_file.read().decode('utf-8')
    stderr = ''.join(progress)
    if process.returncode:
        detail = stderr.strip() or stdout.strip()
        raise RuntimeError(f'Hancom export failed: {detail}')
    return json.loads(stdout)


def export_pdf(source, output, timeout=300, security_module=None):
    started = time.monotonic()
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
            'elapsed_seconds': round(time.monotonic() - started, 2),
            'pdf_signature_verified': True, 'visual_equivalence_verified': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path, help='New .pdf output path')
    parser.add_argument('--timeout', type=float, default=300, help='Export timeout in seconds (default: 300)')
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
