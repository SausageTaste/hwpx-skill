#!/usr/bin/env python3
"""Start or inspect a detached local Hancom PDF export on Windows."""
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from export_pdf import export_pdf, validate_paths


def now():
    return datetime.now(timezone.utc).isoformat()


def write_status(directory, status):
    # Readers see a complete old or new status, never a partially written JSON.
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory,
                                     suffix='.tmp', delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(status, stream, indent=2)
        stream.write('\n')
    try:
        os.replace(temporary, directory / 'status.json')
    finally:
        temporary.unlink(missing_ok=True)


def read_status(directory):
    directory = Path(directory).resolve(strict=True)
    status = json.loads((directory / 'status.json').read_text(encoding='utf-8'))
    status['job_directory'] = str(directory)
    log = directory / 'progress.log'
    if log.exists():
        with log.open('rb') as stream:
            stream.seek(max(0, log.stat().st_size - 4096))
            status['recent_progress'] = stream.read().decode('utf-8', errors='replace').splitlines()[-8:]
    return status


def run_job(directory):
    directory = Path(directory).resolve(strict=True)
    with (directory / 'runner.pid').open('x', encoding='ascii') as lock:
        lock.write(str(os.getpid()))
    config = json.loads((directory / 'job.json').read_text(encoding='utf-8'))
    status = {**config, 'state': 'running', 'pid': os.getpid(), 'started_utc': now()}
    write_status(directory, status)
    try:
        result = export_pdf(config['source'], config['output'],
                            config['timeout_seconds'], config['security_module'])
    except Exception as error:
        status.update(state='failed', error=str(error), finished_utc=now())
        write_status(directory, status)
        print(str(error), file=sys.stderr, flush=True)
        return 2
    status.update(state='succeeded', result=result, finished_utc=now())
    write_status(directory, status)
    return 0


def start_job(source, output, directory, timeout=86400, wait=30, security_module=None):
    if sys.platform != 'win32':
        raise RuntimeError('Detached Hancom export requires Windows')
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Timeout must be a positive finite number of seconds')
    if not math.isfinite(wait) or not 0 <= wait <= 60:
        raise ValueError('Initial wait must be between 0 and 60 seconds')
    source, output = validate_paths(source, output)
    directory = Path(directory).resolve()
    if directory == output or directory in output.parents or output in directory.parents:
        raise ValueError('PDF output must be separate from the job directory')
    directory.mkdir(parents=True, exist_ok=False)
    config = {'source': str(source), 'output': str(output), 'timeout_seconds': timeout,
              'security_module': security_module, 'created_utc': now()}
    (directory / 'job.json').write_text(json.dumps(config, indent=2), encoding='utf-8')
    write_status(directory, {**config, 'state': 'queued'})
    command = [sys.executable, '-B', str(Path(__file__).resolve()), '_run', str(directory)]
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    try:
        with (directory / 'progress.log').open('xb') as log:
            # No inherited console or tool-session pipes. Breakaway prevents a
            # launcher job object's shutdown from killing this detached job.
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                cwd=directory, env=env, close_fds=True,
                creationflags=(subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                               | subprocess.CREATE_BREAKAWAY_FROM_JOB))
    except Exception as error:
        write_status(directory, {**config, 'state': 'failed', 'error': str(error),
                                 'finished_utc': now()})
        raise
    (directory / 'launcher.json').write_text(json.dumps({'pid': process.pid}), encoding='utf-8')
    try:
        process.wait(timeout=wait)
    except subprocess.TimeoutExpired:
        pass  # Leaving this function must not stop the detached exporter.
    status = read_status(directory)
    if process.poll() is not None and status['state'] in ('queued', 'running'):
        status.update(state='interrupted', error='Job exited without a final status', finished_utc=now())
        write_status(directory, status)
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    start = sub.add_parser('start', help='Launch detached export and return after a short initial check')
    start.add_argument('source', type=Path)
    start.add_argument('output', type=Path)
    start.add_argument('job_directory', type=Path, help='New directory for job status and logs')
    start.add_argument('--timeout', type=float, default=86400, help='Worker limit in seconds (default: 86400)')
    start.add_argument('--wait', type=float, default=30, help='Initial check, 0-60 seconds (default: 30)')
    start.add_argument('--security-module')
    sub.add_parser('status', help='Read saved job status and recent progress').add_argument('job_directory', type=Path)
    sub.add_parser('_run', help=argparse.SUPPRESS).add_argument('job_directory', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == '_run':
            return run_job(args.job_directory)
        if args.command == 'start':
            status = start_job(args.source, args.output, args.job_directory,
                               args.timeout, args.wait, args.security_module)
        else:
            status = read_status(args.job_directory)
    except Exception as error:
        parser.exit(2, f'Error: {error}\n')
    print(json.dumps(status))
    # A pending job is successfully launched, not a successful conversion.
    return 2 if status['state'] in ('failed', 'interrupted') else 0


if __name__ == '__main__':
    raise SystemExit(main())
