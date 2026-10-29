"""Bounded, explicit existing-session transport; no credentials or retries."""
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import time
import uuid
MAX_BYTES = 8 * 1024 * 1024
MAX_FRAME_BYTES = 1024 * 1024
RECEIPT_DIRNAME = 'roundtable-requests'
RECEIPT_SCHEMA = 'roundtable-request-receipt/v1'
RECEIPT_TTL_SECONDS = 24 * 60 * 60
_PASSIVE_ATTACHMENTS = frozenset({
    'hook_success', 'hook_cancelled', 'hook_additional_context', 'hook_blocking_error',
    'hook_system_message', 'environment', 'model', 'language', 'deferred_tools_delta',
    'agent_listing_delta', 'mcp_instructions_delta', 'skill_listing', 'auto_mode',
    'instructions', 'session_context', 'date', 'credential_org', 'remote_session_change',
    'prompt_snapshot', 'silent_turn_reminder', 'nested_memory', 'diagnostics',
    'edited_text_file', 'deferred_tools_record',
})
def _uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("explicit canonical UUID required")
    return value
def _safe(path, kind):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError("absolute safe path required")
    # macOS owns this well-known root alias; no arbitrary symlink resolution.
    if sys.platform == 'darwin' and path.parts[:2] == ('/', 'tmp'):
        alias = Path('/tmp')
        if (alias.is_symlink() and alias.lstat().st_uid == 0
                and os.readlink(alias) in ('private/tmp', '/private/tmp')):
            path = Path('/private/tmp').joinpath(*path.parts[2:])
    for parent in reversed(path.parents):
        info = parent.lstat()
        sticky = info.st_mode & stat.S_ISVTX and info.st_uid == 0
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid())
                or (info.st_mode & 0o022 and not sticky)):
            raise ValueError("unsafe directory")
    info = path.lstat()
    check = {'file': stat.S_ISREG, 'socket': stat.S_ISSOCK, 'dir': stat.S_ISDIR}[kind]
    if not check(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError("unsafe target")
    return info
def _read(path, limit):
    _safe(path, 'file')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("unsafe opened file")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("file exceeds bound")
    return data
def _proc_start(pid):
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='],
                            capture_output=True, text=True, timeout=5,
                            env={**os.environ, 'TZ': 'UTC', 'LC_ALL': 'C'}, check=False)
    return result.stdout.strip() if result.returncode == 0 else ''
def resolve_target(session_id, config_dir=None):
    """Resolve one live Desktop registry entry and freeze transcript identity."""
    if sys.platform not in ('darwin', 'linux'):
        raise ValueError('claude-socket supports macOS and Linux only')
    _uuid(session_id)
    config = Path(config_dir) if config_dir is not None else Path.home() / '.claude'
    sessions, projects = config / 'sessions', config / 'projects'
    _safe(sessions, 'dir')
    _safe(projects, 'dir')
    found = []
    for entry in sessions.glob('*.json'):
        if not entry.stem.isdecimal():
            continue
        try:
            record = json.loads(_read(entry, 65536))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or record.get('sessionId') != session_id:
            continue
        pid = int(entry.stem)
        if pid <= 0 or record.get('pid', pid) != pid:
            continue
        start = _proc_start(pid)
        if start and start == record.get('procStart'):
            found.append((pid, start, record))
    if len(found) != 1:
        raise ValueError("session must have exactly one live registry entry")
    pid, start, record = found[0]
    if record.get('entrypoint') != 'claude-desktop':
        raise ValueError("not a Claude Desktop session")
    sock = record.get('messagingSocketPath')
    if not isinstance(sock, str):
        raise ValueError("missing socket")
    sock_info = _safe(sock, 'socket')
    # Search only exact transcript filenames, never key or credential files.
    transcripts = list(projects.glob(f'*/{session_id}.jsonl'))
    if len(transcripts) != 1:
        raise ValueError("missing or ambiguous transcript")
    transcript = transcripts[0]
    cwd = record.get('cwd')
    if isinstance(cwd, str):
        expected = projects / re.sub(r'[^A-Za-z0-9]', '-', cwd) / f'{session_id}.jsonl'
        if expected.exists() and expected != transcript:
            raise ValueError("ambiguous transcript")
    info = _safe(transcript, 'file')
    return dict(session_id=session_id, pid=pid, socket_path=sock, proc_start=start,
                transcript_path=str(transcript), transcript_device=info.st_dev,
                transcript_inode=info.st_ino, transcript_offset=info.st_size,
                config_dir=str(config), socket_device=sock_info.st_dev,
                socket_inode=sock_info.st_ino)
def revalidate_target(target):
    current = resolve_target(target['session_id'], target.get('config_dir'))
    for key in ('pid', 'proc_start', 'socket_path', 'socket_device', 'socket_inode',
                'transcript_path', 'transcript_device', 'transcript_inode'):
        if current[key] != target[key]:
            raise ValueError("target identity changed")
    offset = target['transcript_offset']
    if type(offset) is not int or offset < 0 or current['transcript_offset'] < offset:
        raise ValueError("transcript truncated or invalid offset")
    return current
def _prune_receipts(root, now):
    """Remove our own receipt files older than the receipt lifetime; never follow links."""
    for entry in root.iterdir():
        if not re.fullmatch(r'cc-msg-[0-9a-f]{32}\.(json|consumed)', entry.name):
            continue
        info = entry.lstat()
        if (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and info.st_mtime < now - RECEIPT_TTL_SECONDS):
            entry.unlink(missing_ok=True)
def issue_receipt(target, invocation_id, participant, message_id, now=None):
    """Write the one-shot request receipt the local output gate verifies before exempting a reply."""
    if not re.fullmatch(r'cc-msg-[0-9a-f]{32}', message_id or ''):
        raise ValueError("canonical message_id required")
    if not re.fullmatch(r'[0-9a-f]{12}', invocation_id or '') or not participant:
        raise ValueError("invocation_id and participant required")
    config = target.get('config_dir')
    if not isinstance(config, str):
        raise ValueError("target config_dir required")
    root = Path(config) / 'runtime' / RECEIPT_DIRNAME
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _safe(root, 'dir')
    issued = int(time.time() if now is None else now)
    _prune_receipts(root, issued)
    body = dict(schema=RECEIPT_SCHEMA, message_id=message_id, session_id=_uuid(target['session_id']),
                invocation_id=invocation_id, participant=participant, sender_pid=os.getpid(),
                issued_at=issued, expires_at=issued + RECEIPT_TTL_SECONDS)
    path = root / f'{message_id}.json'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(body, stream, ensure_ascii=False)
    return str(path)
def send(target, text, message_id):
    """Write exactly one frame. A write is not a model receipt."""
    if not isinstance(text, str) or not isinstance(message_id, str) or not message_id:
        raise ValueError("text and message_id required")
    revalidate_target(target)
    frame = dict(msgV=1, msg_id=message_id, type='user',
                 message=dict(role='user', content=(
                     '<cross-session-message from-name="AI Round Table">\n'
                     + text.replace('<', '&lt;') + '\n</cross-session-message>')),
                 priority='next', session_id=target['session_id'])
    data = (json.dumps(frame, ensure_ascii=False) + '\n').encode('utf-8')
    if len(data) > MAX_FRAME_BYTES:
        raise ValueError("frame exceeds bound")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(5)
        peer.connect(target['socket_path'])
        peer.sendall(data)
        peer.shutdown(socket.SHUT_WR)
        feedback, deadline = b'', time.monotonic() + 5
        try:
            while len(feedback) < 4096 and b'\n' not in feedback:
                peer.settimeout(max(0.001, deadline - time.monotonic()))
                chunk = peer.recv(4096 - len(feedback))
                if not chunk:
                    break
                feedback += chunk
                if time.monotonic() >= deadline:
                    break
        except (TimeoutError, OSError):
            pass
    rejected = False
    for line in feedback.splitlines():
        try:
            item = json.loads(line)
            rejected |= isinstance(item, dict) and bool(
                item.get('error') or item.get('refused') or item.get('denied')
                or item.get('status') in ('error', 'refused', 'denied', 'rejected', 'unauthorized')
                or item.get('type') in ('error', 'refused', 'denied'))
        except (ValueError, TypeError):
            pass
    return dict(state='transport-rejected' if rejected else 'frame-written',
                reason='peer-rejected' if rejected else 'delivery-unverified',
                delivery_verified=False, message_id=message_id, session_id=target['session_id'])
def _text(message):
    content = message.get('content')
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(isinstance(item, dict) for item in content):
        return '\n'.join(item.get('text', '') for item in content
                         if item.get('type') == 'text' and isinstance(item.get('text'), str))
    return ''
def reply(target, invocation_id, participant):
    """Return correlated final end_turn JSON; unknown completion stays pending."""
    try:
        if not isinstance(invocation_id, str) or not re.fullmatch(r'[0-9a-f]{12}', invocation_id):
            return None
        marker = target.get('request_marker')
        if not isinstance(marker, str) or not marker or not isinstance(participant, str):
            return None
        current = revalidate_target(target)
        offset = target['transcript_offset']
        length = current['transcript_offset'] - offset
        if length > MAX_BYTES:
            return None
        path = Path(target['transcript_path'])
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (info.st_dev, info.st_ino) != (target['transcript_device'], target['transcript_inode']):
                return None
            if offset:
                stream.seek(offset - 1)
                if stream.read(1) != b'\n':
                    return None
            stream.seek(offset)
            data = stream.read(length)
        if data and not data.endswith(b'\n'):
            return None
        active, candidate = False, None
        pending_id, pending_text = None, ''
        for line in data.splitlines():
            record = json.loads(line)
            if not isinstance(record, dict):
                return None
            if record.get('type') == 'attachment':
                attachment = record.get('attachment', {})
                if record.get('sessionId') != target['session_id']:
                    return None
                if not isinstance(attachment, dict):
                    return None
                if isinstance(attachment, dict) and attachment.get('type') == 'queued_command':
                    if record.get('sessionId') != target['session_id']:
                        return None
                    prompt = attachment.get('prompt', '')
                    if not isinstance(prompt, str):
                        return None
                    origin = attachment.get('origin', {})
                    if (not active and isinstance(origin, dict) and origin.get('kind') == 'peer'
                            and prompt.count(marker) == 1 and invocation_id in prompt
                            and re.search(r'(?<![\w-])' + re.escape(marker) + r'(?![\w-])', prompt)):
                        active = True
                    elif active:
                        return None
                elif active and attachment.get('type') not in _PASSIVE_ATTACHMENTS:
                    return None
                continue
            if record.get('type') not in ('user', 'assistant'):
                continue
            if record.get('sessionId') != target['session_id']:
                return None
            message = record.get('message', {})
            if not isinstance(message, dict):
                return None
            text = _text(message)
            if record.get('type') == 'user':
                content = message.get('content')
                if (isinstance(content, list) and content
                        and all(isinstance(item, dict) and item.get('type') == 'tool_result'
                                for item in content)):
                    continue
                if (active and record.get('isMeta') is True
                        and text.startswith('Stop hook feedback:\n')):
                    # The native output gate rejected the previous answer.
                    # Await its corrected final answer; never collect the rejected one.
                    candidate, pending_id, pending_text = None, None, ''
                    continue
                if active:
                    return None  # Even a matching second user record is ambiguous.
                if (message.get('role') == 'user' and text.count(marker) == 1
                        and re.search(r'(?<![\w-])' + re.escape(marker) + r'(?![\w-])', text)
                        and invocation_id in text):
                    active = True
                continue
            if record.get('type') != 'assistant' or not active:
                continue
            candidate = None
            if any(item.get('type') == 'tool_use' for item in message.get('content', [])
                   if isinstance(item, dict)) or message.get('stop_reason') == 'tool_use':
                pending_id, pending_text = None, ''
                continue  # A busy session may finish its existing tools before replying.
            api_id = message.get('id')
            if not isinstance(api_id, str) or not api_id.strip():
                api_id = None
            if api_id is None or api_id != pending_id:
                pending_id, pending_text = None, ''
            if message.get('role') != 'assistant' or 'stop_reason' not in message:
                return None
            if message['stop_reason'] is None:
                if api_id and text.strip():
                    pending_id, pending_text = api_id, text
                continue
            if message['stop_reason'] != 'end_turn':
                pending_id, pending_text = None, ''
                continue
            if not text.strip() and api_id and api_id == pending_id:
                text = pending_text
            pending_id, pending_text = None, ''
            if not text.strip():
                continue
            response_uuid = _uuid(record.get('uuid'))
            text = text.strip()
            if text.startswith('```json\n') and text.endswith('\n```'):
                text = text[8:-4]
            elif text.startswith('```\n') and text.endswith('\n```'):
                text = text[4:-4]
            if invocation_id not in text:
                continue
            try:
                result = json.loads(text)
            except json.JSONDecodeError:
                continue  # A native output hook can reject this answer and request a correction.
            if (isinstance(result, dict) and result.get('invocation_id') == invocation_id
                    and result.get('participant') == participant):
                candidate = dict(text=json.dumps(result, ensure_ascii=False),
                                 response_uuid=response_uuid, session_id=target['session_id'])
        if revalidate_target(target)['transcript_offset'] != current['transcript_offset']:
            return None
        return candidate
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        return None
