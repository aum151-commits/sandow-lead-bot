"""Opaque cross-channel journey pointers. No bot imports, network or dependencies."""
import hashlib
import re
import secrets
import threading
import time
from collections import deque
from attribution import sanitize_attribution, is_test

TOKEN_RE = re.compile(r'j_[0-9a-f]{24}')
LEGACY_RE = re.compile(r'[a-z0-9_-]{1,32}')
JOURNEY_TTL = 7 * 86400
ATTR_TTL = 30 * 86400
ORIGINS = frozenset(('https://sandowfitness.ru', 'https://lp.sandowfitness.ru', 'https://www.sandowfitness.ru'))

def clean_attribution(value):
    value = value if isinstance(value, dict) else {}
    flat = {'sndw_' + k: v for k, v in value.items() if k != 'path'}
    flat['sndw_path'] = value.get('path', [])
    return sanitize_attribution(flat)

def unknown_attribution():
    return sanitize_attribution({})

def token_path(token):
    return 'data/site_journeys/' + token + '.json' if isinstance(token, str) and TOKEN_RE.fullmatch(token) else None

def parse_payload(payload):
    """Return strict base pointer and safe optional legacy theme, never lowercase a token."""
    if not isinstance(payload, str): return None, ''
    match = re.fullmatch(r'(j_[0-9a-f]{24})(?:_([a-z0-9_-]{1,32}))?', payload)
    return (match.group(1), match.group(2) or '') if match else (None, '')


def create_journey(data, load, save, now=None):
    """Return {token,...} only after verified storage; adapters use (path,default)/(path,obj,message)."""
    now = time.time() if now is None else now
    if not isinstance(data, dict): return None
    if is_test(data): return {'test': True}
    channel = data.get('channel')
    legacy = data.get('legacy_start', '')
    if channel not in ('telegram', 'max') or not isinstance(legacy, str) or (legacy and not LEGACY_RE.fullmatch(legacy)):
        return None
    supplied = data.get('token', '')
    if not isinstance(supplied, str): return None
    if supplied and re.fullmatch(r'[0-9a-f]{24}', supplied): supplied = 'j_' + supplied
    if supplied and not TOKEN_RE.fullmatch(supplied): return None
    token = supplied or 'j_' + secrets.token_hex(12)
    record = {'version': 1, 'channel': channel, 'legacy_start': legacy,
              'created_at': now, 'expires_at': now + JOURNEY_TTL,
              'attribution': sanitize_attribution(data)}
    try:
        path = token_path(token)
        existing = load(path, None)
        if existing is not None:
            resolved = resolve_journey(token, channel, load, now)
            if not resolved or any(resolved.get(k) != record[k] for k in ('channel', 'legacy_start', 'attribution')): return None
            record = resolved
        else:
            save(path, record, 'site journey created')
            if load(path, None) != record: return None
    except Exception:
        return None
    return {'token': token, 'payload': token + ('_' + legacy if legacy else ''), 'expires_at': record['expires_at'], 'channel': channel}

def resolve_journey(token, channel, load, now=None):
    """Strict lower-case token/channel/expiry. None means unknown, never a guessed source."""
    now = time.time() if now is None else now
    path = token_path(token)
    if not path or channel not in ('telegram', 'max'): return None
    try:
        rec = load(path, None)
        if not isinstance(rec, dict) or rec.get('channel') != channel or rec.get('version') != 1: return None
        created, expires = float(rec['created_at']), float(rec['expires_at'])
        if not (0 <= created <= now < expires <= created + JOURNEY_TTL): return None
        legacy = rec.get('legacy_start', '')
        if not isinstance(legacy, str) or (legacy and not LEGACY_RE.fullmatch(legacy)): return None
        return {**rec, 'attribution': clean_attribution(rec.get('attribution'))}
    except (ValueError, TypeError, KeyError, OverflowError):
        return None
    except Exception:
        return None

def subscriber_attribution(record, now=None):
    """30-day evidence window; old or malformed records become explicit unknown."""
    now = time.time() if now is None else now
    if not isinstance(record, dict): return unknown_attribution()
    try:
        seen = float(record['attribution_saved_at'])
        if not (0 <= seen <= now < seen + ATTR_TTL): return unknown_attribution()
    except (ValueError, TypeError, KeyError, OverflowError): return unknown_attribution()
    return clean_attribution(record.get('attribution'))

def apply_journey(record, resolved, now=None):
    """Mutate existing subscriber only on a successfully resolved explicit click.

    Fields shared by Telegram and MAX: attribution, attribution_saved_at.
    An untagged /start MUST NOT call this with fabricated evidence or refresh TTL.
    """
    if not isinstance(resolved, dict): return record
    now = time.time() if now is None else now
    record['attribution'] = clean_attribution(resolved.get('attribution'))
    record['attribution_saved_at'] = now
    return record

class JourneyRateLimiter:
    """Bounded per-process minute limiter; salted peer hashes only, no raw IPs kept.

    A real site can send several legitimate bot taps per minute, so the defaults
    are generous: a low ceiling here would silently turn journeys into unknown.
    """
    def __init__(self, per_minute=600, per_peer=20):
        self.lock = threading.Lock()
        self.salt = secrets.token_bytes(16)
        self.limit = per_minute
        self.peer_limit = per_peer
        self.events = deque(maxlen=per_minute)
        self.peers = {}

    def allow(self, peer, now=None):
        now = time.monotonic() if now is None else now
        key = hashlib.sha256(self.salt + str(peer or '').encode('utf-8')[:256]).digest()
        with self.lock:
            while self.events and self.events[0] <= now - 60: self.events.popleft()
            self.peers = {k: deque(t for t in times if t > now - 60) for k, times in self.peers.items() if times and times[-1] > now - 60}
            times = self.peers.get(key, deque())
            if len(self.events) >= self.limit or len(times) >= self.peer_limit: return False
            self.events.append(now)
            times.append(now)
            self.peers[key] = times
            return True
