"""Local synthetic text ingestion. Only the pseudonym, never its salt, is returned."""
from datetime import datetime, timezone
import hashlib
import hmac
import os
from pathlib import Path
import re
import stat
import unicodedata

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'


def fixture_path(name: str = 'handoff_2') -> Path:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}(?:\.txt)?', name):
        raise ValueError('Fixture must be a safe bare name or .txt filename')
    filename = name if name.endswith('.txt') else name + '.txt'
    root = FIXTURES.resolve()
    path = (root / filename).resolve()
    if path.parent != root or not path.is_file():
        raise ValueError('Requested text fixture is unavailable')
    return path


def local_salt() -> bytes:
    path = Path(os.getenv('HANDOFF_PSEUDONYM_SALT_FILE', '~/.handoff/pseudonym-salt')).expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(os.urandom(32))
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError('Local pseudonym salt must be a private regular file (mode 0600)')
        value = stream.read(33)
    if len(value) != 32:
        raise ValueError('Local pseudonym salt must contain exactly 32 bytes')
    return value


def load_recording(name: str = 'handoff_2') -> dict:
    path = fixture_path(name)
    if path.stat().st_size > 1_000_000:
        raise ValueError('Fixture exceeds the 1 MB demo input limit')
    transcript = path.read_text(encoding='utf-8').strip()
    if not transcript:
        raise ValueError('Fixture is empty')
    return recording_from_text(transcript)


def recording_from_text(transcript: str, source: str = 'synthetic text fixture; not clinical patient data') -> dict:
    """Assign the same local pseudonym for fixtures and operator-uploaded text."""
    from hallway.common.brief import extract_identifiers
    transcript=transcript.strip()
    if not transcript:
        raise ValueError('Transcript is empty')
    identifiers = sorted({unicodedata.normalize('NFKC', value).casefold().strip()
                          for value in extract_identifiers(transcript) if value.strip()})
    if not identifiers:
        raise ValueError('Cannot assign stable pseudonym without recognized direct identifiers')
    # Length-prefix prevents ambiguous concatenations. No identity map is persisted.
    canonical = ''.join(f'{len(value)}:{value}' for value in identifiers).encode('utf-8')
    pseudo_id = 'p_' + hmac.new(local_salt(), canonical, hashlib.sha256).hexdigest()
    return {'transcript': transcript, 'pseudo_id': pseudo_id,
            'source': source,
            'at': datetime.now(timezone.utc).isoformat()}
