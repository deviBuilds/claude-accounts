"""Read-only subscription usage, with a conservative per-account disk cache."""
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request

from desktop_profiles import atomic_json

TTL = 300
ENDPOINT = 'https://api.anthropic.com/api/oauth/usage'


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def reset_time(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def normalize(data):
    windows = []
    for row in data.get('limits', []) or []:
        if not isinstance(row, dict) or not number(row.get('percent')):
            continue
        kind = row.get('kind', '')
        scope = row.get('scope') or {}
        if not isinstance(scope, dict):
            scope = {}
        labels = []
        for key in ('model', 'surface'):
            value = scope.get(key)
            if isinstance(value, dict):
                value = value.get('display_name') or value.get('id')
            if isinstance(value, str):
                labels.append(''.join(c for c in value if c.isprintable())[:40])
        label = {'session': '5-hour session', 'weekly_all': 'Weekly — all models',
                 'weekly_scoped': 'Weekly — ' + (' / '.join(labels) or 'model limit')}.get(kind)
        if label:
            windows.append({'key': kind, 'label': label, 'percent': row['percent'], 'resets_at': reset_time(row.get('resets_at'))})
    # Older versions of the service have only these top-level windows.
    for key, kind, label in [('five_hour', 'session', '5-hour session'), ('seven_day', 'weekly_all', 'Weekly — all models')]:
        row = data.get(key)
        if not any(w['key'] == kind for w in windows) and isinstance(row, dict) and number(row.get('utilization')):
            windows.append({'key': kind, 'label': label, 'percent': row['utilization'], 'resets_at': reset_time(row.get('resets_at'))})
    if not any(w['key'] == 'weekly_scoped' for w in windows):
        for key, row in data.items():
            if key.startswith('seven_day_') and isinstance(row, dict) and number(row.get('utilization')):
                windows.append({'key': key, 'label': 'Weekly — '+key.removeprefix('seven_day_').replace('_', ' ').title(),
                                'percent': row['utilization'], 'resets_at': reset_time(row.get('resets_at'))})
    return sorted(windows, key=lambda w: {'session': 0, 'weekly_all': 1}.get(w['key'], 2))


def fetch(token):
    request = urllib.request.Request(ENDPOINT, headers={
        'Authorization': 'Bearer ' + token, 'anthropic-beta': 'oauth-2025-04-20'})
    with urllib.request.urlopen(request, timeout=10) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError('Invalid usage response')
    return normalize(value)


def load(home, account, credential=None, now=None, fetcher=fetch):
    now = time.time() if now is None else now
    if not account or not re.fullmatch(r'[a-zA-Z0-9-]+', account):
        return {'state': 'unavailable', 'windows': [], 'detail': 'Verify CLI login to load usage.'}
    root = home / '.claude-account-usage'
    if root.is_symlink():
        return {'state': 'unavailable', 'windows': [], 'detail': 'Usage cache directory must not be a symlink.'}
    path = root / (account + '.json')
    try:
        cached = json.loads(path.read_text())
        if not isinstance(cached, dict) or not isinstance(cached.get('windows'), list):
            cached = {}
    except (OSError, ValueError):
        cached = {}
    if cached.get('next_check', 0) > now:
        return cached
    if credential is None:
        return {**cached, 'state': 'stale' if cached.get('windows') else 'unavailable',
                'windows': cached.get('windows', []), 'detail': 'Waiting for a valid CLI login.'}
    next_check, error = now+TTL, None
    try:
        windows = fetcher(credential['accessToken'])
        result = {'state': 'ready' if windows else 'unavailable', 'windows': windows,
                  'checked_at': now, 'next_check': next_check, 'detail': '' if windows else 'No usage limits returned for this account.'}
    except urllib.error.HTTPError as failure:
        if failure.code == 429:
            try:
                delay = float(failure.headers.get('Retry-After', 300))
                if not math.isfinite(delay):
                    delay = TTL
            except (ValueError, TypeError):
                delay = TTL
            next_check = now + max(60, min(delay, 3600))
            error = 'Usage service rate-limited; retrying later.'
        else:
            error = f'Usage unavailable (HTTP {failure.code}).'
        failure.close()
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        error = 'Usage service is unavailable; retrying later.'
    if error:
        result = {**cached, 'state': 'stale' if cached.get('windows') else 'unavailable',
                  'windows': cached.get('windows', []), 'detail': error, 'next_check': next_check}
    try:
        root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(root, 0o700)
        atomic_json(path, result)
    except OSError:
        pass  # Usage display must not break account switching if caching fails.
    return result


def countdown(timestamp, now=None):
    if timestamp is None:
        return 'time unavailable'
    seconds = timestamp - (time.time() if now is None else now)
    if seconds <= 0:
        return 'reset passed'
    minutes = max(1, math.ceil(seconds/60))
    if minutes >= 1440:
        return f'in {minutes//1440}d {(minutes%1440)//60}h'
    return f'in {minutes//60}h {minutes%60}m' if minutes >= 60 else f'in {minutes}m'


def summary(window, now=None):
    stamp = window.get('resets_at')
    label = '5h' if window['key'] == 'session' else '7d'
    if stamp is None:
        return f'{label} {window["percent"]:g}% used · reset time unavailable'
    reset = dt.datetime.fromtimestamp(stamp).astimezone().strftime('%a %I:%M%p').replace('AM', 'am').replace('PM', 'pm')
    return f'{label} {window["percent"]:g}% used · resets {reset} ({countdown(stamp, now)})'


def details(usage):
    lines = ['Percentages are used, not remaining. Reset times use your local timezone.']
    if usage.get('checked_at'):
        checked = dt.datetime.fromtimestamp(usage['checked_at']).astimezone().strftime('%b %d, %I:%M:%S %p %Z')
        lines.append(('Last successful update: ' if usage.get('state') == 'stale' else 'Updated: ') + checked)
    for window in usage.get('windows', []):
        stamp = window.get('resets_at')
        reset = dt.datetime.fromtimestamp(stamp).astimezone().strftime('%a %b %d, %I:%M %p %Z') if stamp else 'not provided'
        lines.append(f'{window["label"]}: {window["percent"]:g}% used. Resets {reset} ({countdown(stamp)}).')
    if usage.get('detail'):
        lines.append(usage['detail'])
    if not usage.get('windows') and not usage.get('detail'):
        lines.append('Usage has not been loaded yet.')
    lines.append('Usage is cached for up to 5 minutes; service errors may delay updates.')
    return lines
