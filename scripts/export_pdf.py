#!/usr/bin/env python3
"""Export HWPX through desktop Hancom with a supervised end-to-end deadline."""
import argparse
import getpass
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
from zipfile import ZipFile

from hwpx_archive import MIMETYPE

EVENT_PREFIX = 'HWPX_PROGRESS '
REPORT_TIMEOUT = 5


class ExportError(RuntimeError):
    def __init__(self, report):
        super().__init__(report['error'])
        self.report = report


def execution_context():
    """Describe the Windows token, not just inherited USERNAME values."""
    account, session = getpass.getuser(), None
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes
        buffer = ctypes.create_unicode_buffer(256)
        size = wintypes.DWORD(len(buffer))
        if ctypes.windll.advapi32.GetUserNameW(buffer, ctypes.byref(size)):
            account = buffer.value
        value = wintypes.DWORD()
        if ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(value)):
            session = value.value
    return {'account': account, 'session_id': session, 'worker_pid': os.getpid()}


def new_report(source, output):
    # abspath is lexical: do not touch possibly blocked files in the supervisor.
    return {'state': 'running', 'source': os.path.abspath(source),
            'output': os.path.abspath(output), 'last_stage': 'Starting worker',
            'elapsed_seconds': 0.0,
            'source_sha256': None, 'output_sha256': None,
            'source_unchanged': None, 'visual_equivalence_verified': False}


class Progress:
    def __init__(self, report):
        self.report = report

    def __call__(self, stage, **details):
        self.report.update(last_stage=stage, **details)
        print(EVENT_PREFIX + json.dumps({'last_stage': stage, **details}),
              file=sys.stderr, flush=True)

    def failure(self):
        if 'failed_stage' not in self.report:
            self(self.report['last_stage'], failed_stage=self.report['last_stage'])


def monitor_staged_size(staged, stop):
    # Filesystem telemetry can block too: keep it inside the supervised worker.
    while not stop.wait(5):
        try:
            size = staged.stat().st_size
        except OSError:
            continue
        if not stop.is_set():
            print(EVENT_PREFIX + json.dumps({'staged_pdf_bytes': size}),
                  file=sys.stderr, flush=True)


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


def validate_report_path(report, source, output):
    if report is None:
        return
    path = Path(report)
    if path.resolve() in (Path(source).resolve(), Path(output).resolve()):
        raise ValueError('Report must be separate from the input and PDF output')
    if path.exists() or path.is_symlink():
        raise ValueError('Report already exists; choose a new path')


def render_with_hancom(source, output, security_module=None, progress=None):
    """Create our own COM object; never attach to an existing active document."""
    import pythoncom
    import win32com.client

    progress = progress or Progress(new_report(source, output))
    pythoncom.CoInitialize()
    hwp = None
    try:
        progress('Starting Hancom COM session')
        hwp = win32com.client.DispatchEx('HWPFrame.HwpObject')
        # Visibility applies to this account's desktop, not necessarily the user's.
        hwp.XHwpWindows.Item(0).Visible = True
        if security_module and not hwp.RegisterModule('FilePathCheckDLL', security_module):
            raise RuntimeError('Hancom did not accept the named security module')
        version = str(hwp.Version)
        progress('Opening HWPX document', renderer='Hancom', renderer_version=version)
        if not hwp.Open(str(source), '', ''):
            raise RuntimeError('Hancom could not open the HWPX document')
        progress('Exporting PDF')
        if not hwp.SaveAs(str(output), 'PDF', ''):
            raise RuntimeError('Hancom PDF export returned failure')
        return {'renderer': 'Hancom', 'renderer_version': version}
    except BaseException:
        progress.failure()
        raise
    finally:
        try:
            if hwp is not None:
                progress('Closing Hancom COM session')
                hwp.Quit()
        finally:
            pythoncom.CoUninitialize()


def publish_new(staged, output):
    # Same-directory staging permits atomic publication without replacing files.
    # Windows rename refuses an existing destination; POSIX rename would replace it.
    if os.name == 'nt':
        os.rename(staged, output)
    else:
        os.link(staged, output)
        staged.unlink()


def export_in_worker(source, output, security_module=None, report_path=None, record=None):
    record = record if record is not None else new_report(source, output)
    progress = Progress(record)
    progress('Checking execution context', **execution_context())
    progress('Validating paths and HWPX archive')
    source, output = validate_paths(source, output)
    validate_report_path(report_path, source, output)
    if sys.platform != 'win32':
        raise RuntimeError('Hancom PDF export requires Windows and desktop Hancom')
    if importlib.util.find_spec('win32com') is None:
        raise RuntimeError('Install pywin32 in this Python environment: python -m pip install pywin32')
    progress('Hashing input')
    before_hash = file_hash(source)
    progress('Preparing output directory', source_sha256=before_hash)
    output.parent.mkdir(parents=True, exist_ok=True)
    progress('Creating staging directory')
    temporary = tempfile.TemporaryDirectory(prefix='.hwpx-pdf-', dir=output.parent,
                                            ignore_cleanup_errors=True)
    staged = Path(temporary.name) / 'export.pdf'
    progress('Staging directory ready', staging_path=str(staged))
    monitor_stop = threading.Event()
    monitor = threading.Thread(target=monitor_staged_size, args=(staged, monitor_stop), daemon=True)
    monitor.start()
    try:
        renderer = render_with_hancom(source, staged, security_module, progress)
        progress('Validating PDF')
        with staged.open('rb') as stream:
            if not stream.read(8).startswith(b'%PDF-'):
                raise RuntimeError('Hancom output has no PDF signature')
            stream.seek(max(0, staged.stat().st_size - 1024))
            if b'%%EOF' not in stream.read():
                raise RuntimeError('Hancom output has no PDF end marker')
        output_hash, output_bytes = file_hash(staged), staged.stat().st_size
        progress('Verifying input preservation')
        unchanged = file_hash(source) == before_hash
        progress('Input preservation checked', source_unchanged=unchanged)
        if not unchanged:
            raise RuntimeError('Input changed during export; PDF was not published')
        progress('Publishing PDF', **renderer, output_sha256=output_hash,
                 output_bytes=output_bytes, pdf_signature_verified=True)
        publish_new(staged, output)
        progress('PDF published', output_published=True)
    except BaseException:
        progress.failure()
        raise
    finally:
        monitor_stop.set()
        monitor.join(timeout=0.1)
        progress('Cleaning staging directory')
        temporary.cleanup()
    progress('Completed')
    record['state'] = 'succeeded'
    return record


def run_worker(source, output, timeout, security_module, report_path=None):
    """Supervise all filesystem/COM work without filesystem polling in the parent."""
    started = time.monotonic()
    record = new_report(source, output)
    command = [sys.executable, '-B', str(Path(__file__).absolute()),
               str(source), str(output), '--_worker']
    if security_module:
        command.extend(['--security-module', security_module])
    if report_path is not None:
        command.extend(['--report', str(report_path)])
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    events = queue.Queue()

    def read_lines(stream, name):
        try:
            for line in stream:
                events.put((name, line))
        finally:
            stream.close()
            events.put((name, None))

    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding='utf-8', env=env)
    record['worker_pid'] = process.pid
    for name, stream in (('stdout', process.stdout), ('stderr', process.stderr)):
        threading.Thread(target=read_lines, args=(stream, name), daemon=True).start()
    stdout, closed = [], set()
    next_heartbeat = started + 15
    try:
        while len(closed) < 2 or process.poll() is None:
            now = time.monotonic()
            remaining = timeout - (now - started)
            if remaining <= 0:
                record.update(state='timed_out', error=(
                    f'Export exceeded the {timeout:g}s time limit at {record["last_stage"]}. '
                    'The Python worker was stopped; Hancom may still be rendering. '
                    'Inspect the recorded account, session, staging path and output before retrying.'))
                raise ExportError(record)
            if now >= next_heartbeat:
                print(f'Export running: {now - started:.0f}s elapsed; '
                      f'last stage: {record["last_stage"]}', file=sys.stderr, flush=True)
                next_heartbeat = now + 15
            try:
                name, line = events.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            if line is None:
                closed.add(name)
            elif name == 'stdout':
                stdout.append(line)
            elif line.startswith(EVENT_PREFIX):
                event = json.loads(line[len(EVENT_PREFIX):])
                record.update(event)
                if 'last_stage' in event:
                    print(record['last_stage'], file=sys.stderr, flush=True)
                    if 'account' in event:
                        print(f'Account: {event["account"]}; session: {event.get("session_id")}',
                              file=sys.stderr, flush=True)
                    if 'staging_path' in event:
                        print(f'Staging path: {event["staging_path"]}', file=sys.stderr, flush=True)
                elif 'staged_pdf_bytes' in event:
                    print(f'Staged PDF: {event["staged_pdf_bytes"]:,} bytes', file=sys.stderr, flush=True)
            else:
                print(line, end='', file=sys.stderr, flush=True)
        try:
            result = json.loads(''.join(stdout))
        except (ValueError, TypeError) as error:
            record.update(state='failed', error=f'Export worker returned no valid result (exit {process.returncode})')
            raise ExportError(record) from error
        record.update(result)
        if process.returncode or record.get('state') != 'succeeded':
            if record.get('state') == 'succeeded':
                record['state'] = 'failed'
            record.setdefault('error', f'Export worker exited with code {process.returncode}')
            raise ExportError(record)
        return record
    finally:
        if process.poll() is None:
            process.kill()
            # Never wait indefinitely for inherited pipe handles or native Hancom.
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        record['elapsed_seconds'] = round(time.monotonic() - started, 2)


def export_pdf(source, output, timeout=300, security_module=None, report_path=None):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Timeout must be a positive finite number of seconds')
    return run_worker(source, output, timeout, security_module, report_path)


def write_report(path, record):
    """Executed in a separate bounded process, including path checks and cleanup."""
    path = Path(path)
    validate_report_path(path, record['source'], record['output'])
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.hwpx-report-', dir=path.parent) as directory:
        staged = Path(directory) / 'report.json'
        staged.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
        publish_new(staged, path)


def save_report(path, record):
    command = [sys.executable, '-B', str(Path(__file__).absolute()), '--_write-report', str(path)]
    # Report persistence must not reintroduce an unbounded directory operation.
    subprocess.run(command, input=json.dumps(record), text=True, encoding='utf-8',
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                   timeout=REPORT_TIMEOUT, check=True)


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == '--_write-report':
        try:
            write_report(argv[1], json.load(sys.stdin))
        except Exception as error:
            print(str(error), file=sys.stderr)
            return 2
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path, help='New .pdf output path')
    parser.add_argument('--timeout', type=float, default=300, help='End-to-end export deadline in seconds')
    parser.add_argument('--security-module', help='Explicit name of an already configured Hancom module')
    parser.add_argument('--report', type=Path, help='New JSON report path; save success or failure diagnostics')
    parser.add_argument('--_worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    started = time.monotonic()
    record = new_report(args.source, args.output)
    code = 0
    try:
        if args._worker:
            record = export_in_worker(args.source, args.output, args.security_module, args.report, record)
        else:
            record = export_pdf(args.source, args.output, args.timeout, args.security_module, args.report)
    except ExportError as error:
        record, code = error.report, 2
    except Exception as error:
        record.update(state='failed', error=str(error), elapsed_seconds=round(time.monotonic() - started, 2),
                      failed_stage=record.get('failed_stage', record['last_stage']))
        code = 2
    if args.report is not None and not args._worker:
        try:
            save_report(args.report, record)
        except Exception as error:
            detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) else str(error)
            record['report_error'] = detail
            print(f'Could not save report: {detail}', file=sys.stderr)
            code = 2
    print(json.dumps(record))
    if code and not args._worker:
        print('Error: ' + record.get('error', record.get('report_error', 'Export failed')), file=sys.stderr)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
