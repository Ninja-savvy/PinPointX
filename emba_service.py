import json
import os
import re
import subprocess
import time
import logging
import shutil
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


_SCRIPT_DIR = Path(__file__).resolve().parent


try:
    from dotenv import load_dotenv
    load_dotenv(str(_SCRIPT_DIR / '.env'), override=True)
except Exception:
    pass

HOST = os.getenv('EMBA_SERVICE_HOST', '0.0.0.0')
PORT = int(os.getenv('EMBA_SERVICE_PORT', '5002'))

EMBA_PATH = os.getenv('EMBA_PATH', '/opt/emba')
EMBA_LOG_DIR = os.getenv('EMBA_LOG_DIR', str(_SCRIPT_DIR / 'emba_logs'))
EMBA_DIFF_LOG_DIR = os.getenv('EMBA_DIFF_LOG_DIR', str(_SCRIPT_DIR / 'emba_diff_logs'))
EMBA_FIRMWARE_DIR = os.getenv('EMBA_FIRMWARE_DIR', str(_SCRIPT_DIR / 'uploads' / 'firmware'))
EMBA_USE_SUDO = os.getenv('EMBA_USE_SUDO', '0').strip().lower() in {'1', 'true', 'yes'}
EMBA_TIMEOUT_SEC = int(os.getenv('EMBA_TIMEOUT_SEC', str(16 * 60 * 60)))
EMBA_DIFF_TIMEOUT_SEC = int(os.getenv('EMBA_DIFF_TIMEOUT_SEC', str(16 * 60 * 60)))
LOG_LEVEL = os.getenv('LOG_LEVEL', 'DEBUG').upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.DEBUG),
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
)
LOGGER = logging.getLogger('pinpointx.emba_service')
SAFE_SESSION_RE = re.compile(r'[^a-zA-Z0-9_-]+')
SAFE_NAME_RE = re.compile(r'[^a-zA-Z0-9._-]+')
_health_log_lock = threading.Lock()
_health_log_emitted = False
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

os.makedirs(EMBA_LOG_DIR, exist_ok=True)
os.makedirs(EMBA_DIFF_LOG_DIR, exist_ok=True)


def _allow_health_log_once() -> bool:

    global _health_log_emitted
    with _health_log_lock:
        if _health_log_emitted:
            return False
        _health_log_emitted = True
        return True


def _clean_output_line(line: str) -> str:
    cleaned = re.sub(r'\033\[[0-9;]*m', '', line or '')
    return cleaned.strip()


def _resolve_emba_executable() -> str | None:
    """Resolve EMBA executable strictly from EMBA_PATH set in .env.
    No fallback paths or PATH/which lookups are used — set EMBA_PATH correctly in .env.
    """
    if not EMBA_PATH:
        LOGGER.warning('EMBA_PATH is not set in .env')
        return None

    candidates = [
        os.path.join(EMBA_PATH, 'emba.sh'),
        os.path.join(EMBA_PATH, 'emba'),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c

    LOGGER.warning(
        'EMBA executable not found under EMBA_PATH=%s  '
        '(checked emba.sh and emba). Update EMBA_PATH in .env.',
        EMBA_PATH,
    )
    return None


def _write_debug_log(path: Path, content: str):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content or '', encoding='utf-8', errors='ignore')
    except Exception:
        pass


def _timeout_value(seconds: int) -> int | None:
    return seconds if seconds > 0 else None


def _timeout_note(label: str, seconds: int) -> str:
    if seconds <= 0:
        return f'{label} timeout is disabled. Check partial logs.'
    minutes = round(seconds / 60)
    hours = round(seconds / 3600, 1)
    return f'{label} timed out after {minutes} minutes ({hours} hours). Check partial logs.'


def _store_job_result(session_id: str, result: dict):
    with _jobs_lock:
        job = _jobs.setdefault(session_id, {})
        job.update(result)
        job['status'] = result.get('status', 'completed')
        job['finished_at'] = time.time()


def _job_snapshot(session_id: str) -> dict:
    with _jobs_lock:
        return dict(_jobs.get(session_id, {}))


def _is_terminal_status(status: str) -> bool:
    return status in {'completed', 'failed', 'timeout', 'error', 'not_installed'}


def _report_index_relative(log_dir: str) -> str:
    root = Path(log_dir)
    if not root.exists():
        return ''
    for filename in ('index.html', 'emba.html'):
        direct = root / filename
        if direct.exists() and direct.is_file():
            return filename
    for filename in ('index.html', 'emba.html'):
        for p in root.rglob(filename):
            if p.is_file():
                return str(p.relative_to(root))
    return ''


def _log_file_count(log_dir: str) -> int:
    root = Path(log_dir)
    if not root.exists():
        return 0
    return sum(1 for p in root.rglob('*') if p.is_file())


def _safe_session_id(session_id: str) -> str:
    safe = SAFE_SESSION_RE.sub('', session_id or '')
    return safe or 'session'


def _safe_filename(name: str, fallback: str = 'firmware.bin') -> str:
    cleaned = SAFE_NAME_RE.sub('_', (name or '').strip())
    cleaned = cleaned.strip('._-')
    if not cleaned:
        cleaned = fallback
    return cleaned[:140]


def _translate_container_path(path: str) -> str:

    if path.startswith('/app/uploads/firmware/'):
        relative = path[len('/app/uploads/firmware/'):]
        host_path = os.path.join(EMBA_FIRMWARE_DIR, relative)
        if os.path.exists(host_path):
            return host_path
    if path.startswith('/app/'):
        host_path = str(_SCRIPT_DIR / path[5:])
        if os.path.exists(host_path):
            return host_path
    return path


def _prepare_emba_input(src_path: str, session_id: str, prefix: str) -> str:

    src_path = _translate_container_path(src_path)
    src = Path(src_path)
    if not src.exists():
        raise FileNotFoundError(f'Input firmware not found: {src_path}')

    safe_sid = _safe_session_id(session_id)
    base_dir = Path('/tmp/emba_inputs') / safe_sid
    base_dir.mkdir(parents=True, exist_ok=True)

    safe_name = _safe_filename(src.name)
    target = base_dir / f'{prefix}_{safe_name}'
    shutil.copy2(src, target)
    return str(target)


def _tail_text_file(path: Path, max_lines: int = 30) -> str:
    try:
        if not path.exists() or not path.is_file():
            return ''
        with path.open('r', encoding='utf-8', errors='ignore') as f:
            return ''.join(f.readlines()[-max_lines:])
    except Exception:
        return ''


def _run_cmd(
    cmd: list[str],
    emba_exec: str,
    timeout_sec: int | None,
    stdout_log_path: Path | None = None,
    stderr_log_path: Path | None = None,
) -> subprocess.CompletedProcess:
    if stdout_log_path is None and stderr_log_path is None:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            cwd=os.path.dirname(emba_exec),
        )

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    stdout_log_path = stdout_log_path or Path(os.devnull)
    stderr_log_path = stderr_log_path or Path(os.devnull)
    stdout_log_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_log_path.parent.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=os.path.dirname(emba_exec),
    )

    def _pump(pipe, sink: list[str], log_path: Path):
        try:
            with log_path.open('w', encoding='utf-8', errors='ignore') as f:
                for line in iter(pipe.readline, ''):
                    sink.append(line)
                    f.write(line)
                    f.flush()
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    threads = [
        threading.Thread(target=_pump, args=(proc.stdout, stdout_lines, stdout_log_path), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, stderr_lines, stderr_log_path), daemon=True),
    ]
    for t in threads:
        t.start()

    try:
        return_code = proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired as e:
        proc.kill()
        for t in threads:
            t.join(timeout=2)
        e.output = ''.join(stdout_lines)
        e.stderr = ''.join(stderr_lines)
        raise

    for t in threads:
        t.join(timeout=2)

    return subprocess.CompletedProcess(
        args=cmd,
        returncode=return_code,
        stdout=''.join(stdout_lines),
        stderr=''.join(stderr_lines),
    )


def _build_emba_static_cmd(
    emba_exec: str,
    log_dir: str,
    firmware_for_emba: str,
) -> list[str]:

    cmd = []
    if EMBA_USE_SUDO:
        cmd.append('sudo')
    cmd.extend([emba_exec, '-f', firmware_for_emba, '-l', log_dir, '-W'])
    return cmd


def _build_emba_diff_cmd(
    emba_exec: str,
    log_dir: str,
    vulnerable_for_emba: str,
    patched_for_emba: str,
) -> list[str]:

    cmd = []
    if EMBA_USE_SUDO:
        cmd.append('sudo')
    cmd.extend([
        emba_exec,
        '-f', vulnerable_for_emba,
        '-o', patched_for_emba,
        '-l', log_dir,
    ])
    return cmd


def _parse_diff_stream(text: str) -> dict:
    highlights: list[str] = []
    cves: set[str] = set()
    added_markers = 0
    removed_markers = 0
    change_markers = 0

    for raw in (text or '').splitlines():
        line = _clean_output_line(raw)
        if not line:
            continue
        line_l = line.lower()

        if line.startswith('+') and not line.startswith('+++'):
            added_markers += 1
        elif line.startswith('-') and not line.startswith('---'):
            removed_markers += 1

        if any(k in line_l for k in (
            'added', 'removed', 'changed', 'difference',
            'only in', 'differ', 'new file', 'deleted'
        )):
            change_markers += 1
            if len(highlights) < 60:
                highlights.append(line[:320])

        for cve in re.findall(r'CVE-\d{4}-\d{4,7}', line, flags=re.IGNORECASE):
            cves.add(cve.upper())

    return {
        'highlights': highlights,
        'cves': sorted(cves),
        'added_markers': added_markers,
        'removed_markers': removed_markers,
        'change_markers': change_markers,
    }


def _collect_emba_diff_artifacts(log_dir: str) -> dict:
    artifact_paths: list[str] = []
    collected_text: list[str] = []
    max_files = 120
    visited = 0

    for p in Path(log_dir).rglob('*'):
        if not p.is_file():
            continue
        visited += 1
        if visited > max_files:
            break

        rel = str(p.relative_to(log_dir))
        rel_l = rel.lower()
        if any(k in rel_l for k in ('diff', 'delta', 'compare')):
            artifact_paths.append(rel)
            if p.suffix.lower() in {'.txt', '.log', '.csv', '.md'}:
                try:
                    collected_text.append(p.read_text(errors='ignore')[:12000])
                except Exception:
                    pass

    merged = _parse_diff_stream('\n'.join(collected_text))
    return {
        'artifact_paths': artifact_paths[:40],
        'artifact_count': len(artifact_paths),
        **merged,
    }


def _run_emba(firmware_path: str, session_id: str) -> dict:
    log_dir = os.path.abspath(f'{EMBA_LOG_DIR}/{session_id}')
    os.makedirs(log_dir, exist_ok=True)

    emba_exec = _resolve_emba_executable()
    if not emba_exec:
        return {
            'status': 'not_installed',
            'note': (
                'EMBA executable not found. '
                f'Checked EMBA_PATH={EMBA_PATH} and PATH.'
            ),
            'summary': {},
            'findings': [],
        }

    try:
        firmware_for_emba = _prepare_emba_input(firmware_path, session_id, 'fw')
        LOGGER.info('EMBA static start sid=%s firmware=%s log_dir=%s', session_id, firmware_path, log_dir)
        LOGGER.debug('EMBA static prepared firmware path: %s', firmware_for_emba)
        cmd = _build_emba_static_cmd(
            emba_exec=emba_exec,
            log_dir=log_dir,
            firmware_for_emba=firmware_for_emba,
        )
        LOGGER.debug('EMBA static command: %s', ' '.join(cmd))
        proc = _run_cmd(
            cmd,
            emba_exec=emba_exec,
            timeout_sec=_timeout_value(EMBA_TIMEOUT_SEC),
            stdout_log_path=Path(log_dir) / '__emba_stdout.log',
            stderr_log_path=Path(log_dir) / '__emba_stderr.log',
        )

        _write_debug_log(Path(log_dir) / '__emba_stdout.log', proc.stdout or '')
        _write_debug_log(Path(log_dir) / '__emba_stderr.log', proc.stderr or '')

        findings = []
        summary = {'critical': 0, 'high': 0, 'medium': 0, 'low': 0}
        cve_list = []
        services = []
        hardcoded = []

        vuln_csv = Path(log_dir) / 'csv_logs' / 's26_kernel_vuln_verifier.csv'
        if vuln_csv.exists():
            for line in vuln_csv.read_text(errors='ignore').splitlines()[1:]:
                parts = line.split(';')
                if len(parts) >= 4:
                    cve_list.append({
                        'cve': parts[0],
                        'component': parts[1],
                        'version': parts[2],
                        'severity': parts[3],
                    })

        agg_file = Path(log_dir) / 'f50_base_aggregator.txt'
        if agg_file.exists():
            text = agg_file.read_text(errors='ignore')
            for line in text.splitlines():
                line_l = line.lower()
                sev = None
                if '[critical]' in line_l:
                    sev = 'critical'
                elif '[high]' in line_l:
                    sev = 'high'
                elif '[medium]' in line_l:
                    sev = 'medium'
                elif '[low]' in line_l:
                    sev = 'low'
                if sev:
                    clean = re.sub(r'\[.*?\]', '', line).strip()
                    if clean:
                        findings.append({'severity': sev, 'description': clean})
                        summary[sev] += 1

        srv_file = Path(log_dir) / 's80_shell_check.txt'
        if srv_file.exists():
            text = srv_file.read_text(errors='ignore')
            for line in text.splitlines():
                if 'found' in line.lower():
                    services.append(line.strip())

        str_file = Path(log_dir) / 's20_shell_find_pid_creds.txt'
        if str_file.exists():
            text = str_file.read_text(errors='ignore')
            for line in text.splitlines():
                if any(k in line.lower() for k in ['password', 'passwd', 'credential', 'secret', 'token']):
                    hardcoded.append(line.strip())

        if not findings and proc.stdout:
            for line in proc.stdout.splitlines():
                line_l = line.lower()
                sev = None
                if '[critical]' in line_l or 'critical:' in line_l:
                    sev = 'critical'
                elif '[high]' in line_l or 'high:' in line_l:
                    sev = 'high'
                elif '[medium]' in line_l or 'medium:' in line_l:
                    sev = 'medium'
                elif '[low]' in line_l or 'low:' in line_l:
                    sev = 'low'
                if sev:
                    clean = re.sub(r'\033\[[0-9;]*m', '', line).strip()
                    clean = re.sub(r'\[.*?\]', '', clean).strip()
                    if clean and len(clean) > 5:
                        findings.append({'severity': sev, 'description': clean})
                        summary[sev] += 1

        file_count = _log_file_count(log_dir)
        report_index_rel = _report_index_relative(log_dir)
        stdout_tail = (proc.stdout or '')[-4000:]
        stderr_tail = (proc.stderr or '')[-3000:]
        note = ''
        if proc.returncode != 0:
            note = (
                f'EMBA exited with code {proc.returncode}. '
                'Check stdout/stderr debug logs in the EMBA log folder.'
            )
        elif file_count == 0:
            note = (
                'EMBA returned but produced no report files in log directory. '
                'Check __emba_stdout.log and __emba_stderr.log for root cause.'
            )

        LOGGER.info(
            'EMBA static done sid=%s return_code=%s files=%s report_index=%s',
            session_id,
            proc.returncode,
            file_count,
            report_index_rel or 'none',
        )
        if stderr_tail:
            LOGGER.debug('EMBA static stderr tail sid=%s: %s', session_id, stderr_tail)
        if stdout_tail:
            LOGGER.debug('EMBA static stdout tail sid=%s: %s', session_id, stdout_tail)

        return {
            'status': 'completed',
            'summary': summary,
            'findings': findings[:50],
            'cve_list': cve_list[:30],
            'services': services[:20],
            'hardcoded': hardcoded[:10],
            'log_dir': log_dir,
            'firmware_input_path': firmware_for_emba,
            'log_file_count': file_count,
            'report_index_rel': report_index_rel,
            'stdout_tail': stdout_tail,
            'stderr_tail': stderr_tail,
            'note': note,
            'return_code': proc.returncode,
        }
    except subprocess.TimeoutExpired:
        LOGGER.warning('EMBA static timeout sid=%s', session_id)
        return {
            'status': 'timeout',
            'note': _timeout_note('EMBA', EMBA_TIMEOUT_SEC),
            'summary': {},
            'findings': [],
        }
    except Exception as e:
        LOGGER.exception('EMBA static error sid=%s: %s', session_id, e)
        return {
            'status': 'error',
            'note': str(e),
            'findings': [],
        }


def _run_emba_diff(vulnerable_firmware_path: str, patched_firmware_path: str, session_id: str) -> dict:
    log_dir = os.path.abspath(f'{EMBA_DIFF_LOG_DIR}/{session_id}')
    os.makedirs(log_dir, exist_ok=True)

    emba_exec = _resolve_emba_executable()
    if not emba_exec:
        return {
            'status': 'not_installed',
            'note': (
                'EMBA executable not found. '
                f'Checked EMBA_PATH={EMBA_PATH} and PATH.'
            ),
            'summary': {},
            'highlights': [],
        }

    started = time.time()
    try:
        vulnerable_for_emba = _prepare_emba_input(vulnerable_firmware_path, session_id, 'vulnerable')
        patched_for_emba = _prepare_emba_input(patched_firmware_path, session_id, 'patched')
        cmd = _build_emba_diff_cmd(
            emba_exec=emba_exec,
            log_dir=log_dir,
            vulnerable_for_emba=vulnerable_for_emba,
            patched_for_emba=patched_for_emba,
        )
        LOGGER.info(
            'EMBA diff start sid=%s vulnerable=%s patched=%s log_dir=%s',
            session_id,
            vulnerable_firmware_path,
            patched_firmware_path,
            log_dir,
        )
        LOGGER.debug('EMBA diff prepared vulnerable path: %s', vulnerable_for_emba)
        LOGGER.debug('EMBA diff prepared patched path: %s', patched_for_emba)
        LOGGER.debug('EMBA diff command: %s', ' '.join(cmd))

        proc = _run_cmd(
            cmd,
            emba_exec=emba_exec,
            timeout_sec=_timeout_value(EMBA_DIFF_TIMEOUT_SEC),
            stdout_log_path=Path(log_dir) / '__emba_diff_stdout.log',
            stderr_log_path=Path(log_dir) / '__emba_diff_stderr.log',
        )
        _write_debug_log(Path(log_dir) / '__emba_diff_stdout.log', proc.stdout or '')
        _write_debug_log(Path(log_dir) / '__emba_diff_stderr.log', proc.stderr or '')

        raw_parse = _parse_diff_stream((proc.stdout or '') + '\n' + (proc.stderr or ''))
        artifacts = _collect_emba_diff_artifacts(log_dir)
        cves = sorted(set(raw_parse['cves']) | set(artifacts['cves']))
        highlights = []
        for line in raw_parse['highlights'] + artifacts['highlights']:
            if line not in highlights:
                highlights.append(line)
            if len(highlights) >= 60:
                break

        summary = {
            'artifact_count': artifacts['artifact_count'],
            'change_markers': raw_parse['change_markers'] + artifacts['change_markers'],
            'added_markers': raw_parse['added_markers'] + artifacts['added_markers'],
            'removed_markers': raw_parse['removed_markers'] + artifacts['removed_markers'],
            'cve_mentions': len(cves),
            'duration_sec': round(time.time() - started, 2),
            'return_code': proc.returncode,
        }
        file_count = _log_file_count(log_dir)
        report_index_rel = _report_index_relative(log_dir)

        note = ''
        if proc.returncode != 0:
            note = (
                f'EMBA diff completed with exit code {proc.returncode}. '
                'Review highlights and logs to validate findings.'
            )
        elif file_count == 0:
            note = (
                'EMBA diff returned but no artifact files were written. '
                'Check __emba_diff_stdout.log and __emba_diff_stderr.log.'
            )

        LOGGER.info(
            'EMBA diff done sid=%s return_code=%s files=%s report_index=%s',
            session_id,
            proc.returncode,
            file_count,
            report_index_rel or 'none',
        )

        return {
            'status': 'completed',
            'summary': summary,
            'highlights': highlights,
            'cves': cves[:40],
            'artifacts': artifacts['artifact_paths'],
            'log_dir': log_dir,
            'vulnerable_input_path': vulnerable_for_emba,
            'patched_input_path': patched_for_emba,
            'log_file_count': file_count,
            'report_index_rel': report_index_rel,
            'stdout_tail': (proc.stdout or '')[-4000:],
            'stderr_tail': (proc.stderr or '')[-2000:],
            'note': note,
        }
    except subprocess.TimeoutExpired:
        LOGGER.warning('EMBA diff timeout sid=%s', session_id)
        return {
            'status': 'timeout',
            'note': _timeout_note('EMBA diff', EMBA_DIFF_TIMEOUT_SEC),
            'summary': {},
            'highlights': [],
        }
    except Exception as e:
        LOGGER.exception('EMBA diff error sid=%s: %s', session_id, e)
        return {
            'status': 'error',
            'note': str(e),
            'summary': {},
            'highlights': [],
        }


class _Handler(BaseHTTPRequestHandler):
    server_version = 'EMBAService/1.0'

    def _json_body(self) -> dict:
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except Exception:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b'{}'
        try:
            data = json.loads(raw.decode('utf-8') or '{}')
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _send(self, code: int, payload: dict):
        try:
            body = json.dumps(payload).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            LOGGER.debug('Client disconnected before response could be written: %s', self.path)

    def log_message(self, format, *args):
        if self.path == '/health' and not getattr(self, '_log_health_request', False):
            return
        LOGGER.debug('HTTP %s - %s', self.client_address[0], format % args)

    def do_GET(self):
        if self.path == '/health':
            self._log_health_request = _allow_health_log_once()
            if self._log_health_request:
                LOGGER.debug('GET %s', self.path)
            emba_exec = _resolve_emba_executable()
            self._send(200, {
                'status': 'ok',
                'emba_exec': emba_exec or '',
                'emba_installed': bool(emba_exec),
            })
            return

        if self.path.startswith('/api/emba/logs/'):
            session_id = urllib.parse.unquote(self.path.rsplit('/', 1)[-1])
            session_id = _safe_session_id(session_id)
            log_dir = Path(EMBA_LOG_DIR) / session_id
            stdout_path = log_dir / '__emba_stdout.log'
            stderr_path = log_dir / '__emba_stderr.log'
            tail = _tail_text_file(stdout_path) or _tail_text_file(stderr_path)
            job = _job_snapshot(session_id)
            status = job.get('status') or ('running' if log_dir.exists() else 'starting')
            response = {
                'status': status,
                'session_id': session_id,
                'tail': tail or 'Initializing EMBA analysis...',
                'log_file_count': _log_file_count(str(log_dir)),
                'report_index_rel': _report_index_relative(str(log_dir)),
            }
            if _is_terminal_status(status):
                response['result'] = {k: v for k, v in job.items() if k != 'status'}
            self._send(200, response)
            return

        if self.path.startswith('/api/emba/diff-logs/'):
            session_id = urllib.parse.unquote(self.path.rsplit('/', 1)[-1])
            session_id = _safe_session_id(session_id)
            log_dir = Path(EMBA_DIFF_LOG_DIR) / session_id
            stdout_path = log_dir / '__emba_diff_stdout.log'
            stderr_path = log_dir / '__emba_diff_stderr.log'
            tail = _tail_text_file(stdout_path) or _tail_text_file(stderr_path)
            job = _job_snapshot(session_id)
            status = job.get('status') or ('running' if log_dir.exists() else 'starting')
            response = {
                'status': status,
                'session_id': session_id,
                'tail': tail or 'Initializing EMBA diff analysis...',
                'log_file_count': _log_file_count(str(log_dir)),
                'report_index_rel': _report_index_relative(str(log_dir)),
            }
            if _is_terminal_status(status):
                response['result'] = {k: v for k, v in job.items() if k != 'status'}
            self._send(200, response)
            return

        self._log_health_request = True
        LOGGER.debug('GET %s', self.path)
        self._send(404, {'status': 'error', 'note': 'Not found'})

    def do_POST(self):
        LOGGER.debug('POST %s', self.path)
        if self.path == '/api/emba/analyze':
            data = self._json_body()
            session_id = _safe_session_id(data.get('session_id') or 'manual')
            firmware_path = _translate_container_path(data.get('firmware_path') or '')
            if not firmware_path:
                self._send(400, {'status': 'error', 'note': 'Missing firmware_path'})
                return
            if not os.path.exists(firmware_path):
                self._send(400, {'status': 'error', 'note': f'Firmware not found: {firmware_path}'})
                return
            with _jobs_lock:
                existing = _jobs.get(session_id, {})
                if existing.get('status') == 'running':
                    self._send(202, {'status': 'accepted', 'session_id': session_id, 'note': 'EMBA job is already running'})
                    return
                _jobs[session_id] = {
                    'status': 'running',
                    'started_at': time.time(),
                    'firmware_path': firmware_path,
                }

            def _bg():
                result = _run_emba(firmware_path=firmware_path, session_id=session_id)
                _store_job_result(session_id, result)

            threading.Thread(target=_bg, daemon=True).start()
            self._send(202, {'status': 'accepted', 'session_id': session_id})
            return

        if self.path == '/api/emba/diff':
            data = self._json_body()
            session_id = _safe_session_id(data.get('session_id') or 'manual_diff')
            vulnerable_firmware_path = _translate_container_path(
                data.get('vulnerable_firmware_path') or data.get('old_firmware_path') or ''
            )
            patched_firmware_path = _translate_container_path(
                data.get('patched_firmware_path') or data.get('new_firmware_path') or ''
            )
            if not vulnerable_firmware_path or not patched_firmware_path:
                self._send(400, {
                    'status': 'error',
                    'note': 'Missing vulnerable_firmware_path or patched_firmware_path',
                })
                return
            if not os.path.exists(vulnerable_firmware_path):
                self._send(400, {'status': 'error', 'note': f'Vulnerable firmware not found: {vulnerable_firmware_path}'})
                return
            if not os.path.exists(patched_firmware_path):
                self._send(400, {'status': 'error', 'note': f'Patched firmware not found: {patched_firmware_path}'})
                return
            with _jobs_lock:
                existing = _jobs.get(session_id, {})
                if existing.get('status') == 'running':
                    self._send(202, {'status': 'accepted', 'session_id': session_id, 'note': 'EMBA diff job is already running'})
                    return
                _jobs[session_id] = {
                    'status': 'running',
                    'started_at': time.time(),
                    'vulnerable_firmware_path': vulnerable_firmware_path,
                    'patched_firmware_path': patched_firmware_path,
                }

            def _bg_diff():
                result = _run_emba_diff(
                    vulnerable_firmware_path=vulnerable_firmware_path,
                    patched_firmware_path=patched_firmware_path,
                    session_id=session_id,
                )
                _store_job_result(session_id, result)

            threading.Thread(target=_bg_diff, daemon=True).start()
            self._send(202, {'status': 'accepted', 'session_id': session_id})
            return

        self._send(404, {'status': 'error', 'note': 'Not found'})


if __name__ == '__main__':
    emba_exec = _resolve_emba_executable()
    LOGGER.info('─' * 60)
    LOGGER.info('EMBA Service starting on %s:%d', HOST, PORT)
    LOGGER.info('  Project root:  %s', _SCRIPT_DIR)
    LOGGER.info('  EMBA_PATH:     %s', EMBA_PATH)
    LOGGER.info('  EMBA exec:     %s', emba_exec or 'NOT FOUND')
    LOGGER.info('  Use sudo:      %s', EMBA_USE_SUDO)
    LOGGER.info('  Firmware dir:  %s', EMBA_FIRMWARE_DIR)
    LOGGER.info('  Log dir:       %s', EMBA_LOG_DIR)
    LOGGER.info('  Diff log dir:  %s', EMBA_DIFF_LOG_DIR)
    LOGGER.info('─' * 60)
    if not emba_exec:
        LOGGER.warning('EMBA not found! Set EMBA_PATH in .env to your EMBA install directory.')
    server = ThreadingHTTPServer((HOST, PORT), _Handler)
    server.serve_forever()
