"""Verified Claude CLI credential imports. Secrets never enter argv or UI output."""
import argparse
import contextlib
import ctypes as ct
from datetime import datetime
import fcntl
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import stat
import sys
import time
import urllib.error
import urllib.request
import uuid

import desktop_profiles as dp
from onboarding import validate

CLIENT_ID = '9d1c250a-e61b-44d9-88ed-5944d1962f5e'
SCOPES = ['user:file_upload', 'user:inference', 'user:mcp_servers',
          'user:plugins', 'user:profile', 'user:sessions:claude_code']
MAX_FILE = 1024 * 1024


class ImportError(RuntimeError):
    """Only deliberately sanitized messages should cross the helper boundary."""


class RejectedLogin(ImportError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def json_object(raw):
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
    except (ValueError, UnicodeError):
        pass
    raise ImportError('Expected a JSON object. No login was changed.')


def normalize_path(value):
    value = value.strip()
    path = Path(value).expanduser()
    try:
        exists = path.is_file()
    except OSError:
        exists = False
    if not exists:
        # Accept both a plain path with spaces and Finder/shell-quoted paths.
        # shlex only splits text; it never executes substitutions or commands.
        try:
            parts = shlex.split(value)
            if len(parts) == 1:
                path = Path(parts[0]).expanduser()
        except ValueError:
            pass
    return path.absolute()


def read_export(path):
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ImportError('Choose a regular JSON file.')
            raw = stream.read(MAX_FILE + 1)
    except OSError:
        raise ImportError('Could not read that file. Check the path and permissions.') from None
    if len(raw) > MAX_FILE:
        raise ImportError('The login file is too large (maximum 1 MB).')
    data = json_object(raw)
    if data.get('disabled') or data.get('type', 'claude') != 'claude':
        raise ImportError('This is not an enabled Claude CLI login export.')
    native = data.get('claudeAiOauth')
    if native is None:
        native = {'accessToken': data.get('access_token'), 'refreshToken': data.get('refresh_token')}
        try:
            expiry = datetime.fromisoformat(data['expired'].replace('Z', '+00:00'))
            if expiry.tzinfo is None:
                raise ValueError()
            native['expiresAt'] = expiry.timestamp() * 1000
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            raise ImportError('The export needs a valid expired timestamp including its timezone.') from None
    if not isinstance(native, dict):
        raise ImportError('Invalid claudeAiOauth credential object.')
    for field in ('accessToken', 'refreshToken'):
        token = native.get(field)
        if not isinstance(token, str) or not token or len(token) > 16384 or any(c.isspace() or not c.isprintable() for c in token):
            raise ImportError('The file must contain both an access token and a refresh token.')
    expiry = native.get('expiresAt')
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
        raise ImportError('Invalid CLI token expiration time.')
    if expiry <= time.time() * 1000:
        raise ImportError('This access token has expired. Export a fresh login or use CLI sign-in.')
    scopes = native.get('scopes', SCOPES)
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes) or not {'user:inference', 'user:profile'}.issubset(scopes):
        raise ImportError('This login is missing the Claude CLI profile/inference scopes.')
    credential = {k: native[k] for k in ('accessToken', 'refreshToken')}
    credential.update(expiresAt=int(expiry), scopes=scopes)
    return data, credential, hashlib.sha256(raw).hexdigest()


def verify_token(token):
    request = urllib.request.Request('https://api.anthropic.com/api/oauth/profile', headers={
        'Authorization': 'Bearer ' + token, 'anthropic-beta': 'oauth-2025-04-20'})
    try:
        # Refuse redirects so a bearer credential never follows a different host.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
            data = json_object(response.read(MAX_FILE))
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise RejectedLogin('Anthropic rejected this CLI login. Export a fresh login or sign in again.') from None
        raise ImportError(f'Anthropic verification returned HTTP {error.code}. Retry later; nothing was imported.') from None
    except (OSError, ValueError):
        raise ImportError('Could not reach Anthropic to verify the login. Retry when connected.') from None
    return verified_profile(data)


def verified_profile(data):
    try:
        account, org = data['account'], data['organization']
        uuid.UUID(account['uuid'])
        uuid.UUID(org['uuid'])
        if data['application']['uuid'] != CLIENT_ID or not isinstance(account['email'], str) or '@' not in account['email']:
            raise ValueError()
        if any(not c.isprintable() for c in account['email']):
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ImportError('The server did not verify an official Claude Code login and account identity.') from None
    return data


def identity(profile):
    return {'account': profile['account']['uuid'], 'organization': profile['organization']['uuid'],
            'email': profile['account']['email']}


def cached_identity(profile):
    account, org = profile['account'], profile['organization']
    result = {'accountUuid': account['uuid'], 'organizationUuid': org['uuid'], 'emailAddress': account['email'],
              'profileFetchedAt': int(time.time() * 1000)}
    for source, mapping in ((account, {'display_name': 'displayName', 'full_name': 'fullName', 'created_at': 'accountCreatedAt'}),
                            (org, {'name': 'organizationName', 'organization_type': 'organizationType', 'billing_type': 'billingType',
                                   'rate_limit_tier': 'organizationRateLimitTier', 'seat_tier': 'seatTier',
                                   'has_extra_usage_enabled': 'hasExtraUsageEnabled', 'subscription_created_at': 'subscriptionCreatedAt',
                                   'cc_onboarding_flags': 'ccOnboardingFlags', 'claude_code_trial_ends_at': 'claudeCodeTrialEndsAt',
                                   'claude_code_trial_duration_days': 'claudeCodeTrialDurationDays', 'plan_display_name': 'planDisplayName'})):
        result.update({target: source[key] for key, target in mapping.items() if key in source})
    return result


class Keychain:
    """Update the existing item's data, retaining its service, account and ACL."""
    def __init__(self, home, label):
        self.service = 'Claude Code-credentials'
        if label != 'a':
            self.service += '-' + hashlib.sha256(str(home / ('.claude-acct-' + label)).encode()).hexdigest()[:8]
        self.service = self.service.encode()
        self.item = ct.c_void_p()
        self.security = ct.CDLL('/System/Library/Frameworks/Security.framework/Security')
        self.core = ct.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.core.CFRelease.argtypes = [ct.c_void_p]
        self.core.CFRelease.restype = None
        functions = {
            'SecKeychainFindGenericPassword': [ct.c_void_p, ct.c_uint32, ct.c_char_p, ct.c_uint32, ct.c_char_p,
                                               ct.POINTER(ct.c_uint32), ct.POINTER(ct.c_void_p), ct.POINTER(ct.c_void_p)],
            'SecKeychainItemFreeContent': [ct.c_void_p, ct.c_void_p],
            'SecKeychainItemModifyAttributesAndData': [ct.c_void_p, ct.c_void_p, ct.c_uint32, ct.c_void_p],
            'SecKeychainAddGenericPassword': [ct.c_void_p, ct.c_uint32, ct.c_char_p, ct.c_uint32, ct.c_char_p,
                                              ct.c_uint32, ct.c_void_p, ct.POINTER(ct.c_void_p)],
            'SecKeychainItemDelete': [ct.c_void_p],
        }
        for name, args in functions.items():
            func = getattr(self.security, name)
            func.argtypes, func.restype = args, ct.c_int32

    def close(self):
        if self.item:
            self.core.CFRelease(self.item)
            self.item = ct.c_void_p()

    def check(self, code):
        if code:
            raise ImportError(f'macOS Keychain could not complete the import (status {code}). Allow Keychain access and retry.')

    def read(self):
        self.close()
        length, data = ct.c_uint32(), ct.c_void_p()
        code = self.security.SecKeychainFindGenericPassword(None, len(self.service), self.service, 0, None,
                                                           ct.byref(length), ct.byref(data), ct.byref(self.item))
        try:
            if code == -25300:  # Item not found; a new profile may legitimately have no item.
                return None
            self.check(code)
            return ct.string_at(data, length.value)
        finally:
            if data:
                self.security.SecKeychainItemFreeContent(None, data)

    def write(self, payload):
        if payload is None:
            if self.item:
                self.check(self.security.SecKeychainItemDelete(self.item))
                self.close()
            return
        buffer = ct.create_string_buffer(payload)
        if self.item:
            self.check(self.security.SecKeychainItemModifyAttributesAndData(self.item, None, len(payload), buffer))
        else:
            account = getpass.getuser().encode()
            self.check(self.security.SecKeychainAddGenericPassword(None, len(self.service), self.service,
                       len(account), account, len(payload), buffer, ct.byref(self.item)))


def decode_keychain(raw):
    if raw is None:
        return {}
    try:
        return json_object(raw)
    except ImportError:
        try:
            return json_object(bytes.fromhex(raw.decode()))
        except (ValueError, UnicodeError):
            raise ImportError('The existing Keychain item is unreadable. No login was changed.') from None


def expected_identity(manager, label, config, old, verifier):
    index = manager.index()
    saved = index.get('profiles', {}).get(label, {})
    if saved.get('account'):
        return saved, 'saved Desktop profile'
    progress = manager.store / ('onboarding-' + label + '.json')
    pinned = json_object(progress.read_bytes()) if progress.exists() else {}
    pending = index.get('pendingLogin', {})
    if not pinned.get('account') and pending.get('label') == label:
        pinned = pending
    if pinned.get('account'):
        return pinned, 'verified setup identity'
    oauth = old.get('claudeAiOauth') or {}
    if oauth.get('accessToken') and oauth.get('expiresAt', 0) > time.time() * 1000:
        # A functioning login outranks stale .claude.json metadata.
        try:
            return identity(verifier(oauth['accessToken'])), 'current CLI login'
        except RejectedLogin:
            pass  # A revoked login can be replaced, still bound by cached identity.
    cached = config.get('oauthAccount') or {}
    if cached.get('accountUuid') or cached.get('emailAddress'):
        return {'account': cached.get('accountUuid'), 'organization': cached.get('organizationUuid'),
                'email': cached.get('emailAddress')}, 'cached profile identity (CLI unavailable)'
    return {}, 'new profile'


def assert_match(expected, actual, source):
    for key in ('account', 'organization', 'email'):
        a, b = expected.get(key), actual.get(key)
        if a and str(a).casefold() != str(b).casefold():
            raise ImportError(f'The imported login does not match this {source}. Select the correct profile or login file. Nothing was changed.')


@contextlib.contextmanager
def locked(manager, label):
    with manager.locked():
        root = manager.home / '.claude-account-refresh'
        if root.is_symlink():
            raise ImportError('The login-refresh directory must not be a symlink.')
        root.mkdir(mode=0o700, exist_ok=True)
        with (root / (label + '.lock')).open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ImportError('This CLI login is refreshing. Wait for it to finish, then retry.') from None
            yield


def import_login(home, label, path, expected=None, *, verifier=verify_token, storage=Keychain, writer=dp.atomic_json):
    validate(label)
    manager = dp.Profiles(home, None, None)
    with locked(manager, label), contextlib.closing(storage(home, label)) as keychain:
        from account_status import discover
        if label not in discover(home, manager.index()):
            raise ImportError('Create or restore this profile before importing a login.')
        if manager.journal.exists():
            raise ImportError('Recover the interrupted Desktop switch from Tools first.')
        directory = home if label == 'a' else home / ('.claude-acct-' + label)
        config_path = directory / '.claude.json'
        if directory.is_symlink() or config_path.is_symlink():
            raise ImportError('The CLI profile and identity file must not be symlinks.')
        if not directory.is_dir():
            raise ImportError('Set up this profile for CLI use before importing a login.')
        raw_config = config_path.read_bytes() if config_path.exists() else None
        config = json_object(raw_config) if raw_config is not None else {}
        exported, credential, file_hash = read_export(path)
        profile = verified_profile(verifier(credential['accessToken']))
        actual = identity(profile)
        assert_match({'account': exported.get('account_uuid'), 'organization': exported.get('organization_uuid'),
                      'email': exported.get('email')}, actual, 'export metadata')
        before = keychain.read()
        old = decode_keychain(before)
        target, source = expected_identity(manager, label, config, old, verifier)
        assert_match(target, actual, source)
        signature = digest({'label': label, 'file': file_hash, 'old': old, 'config': config,
                            'target': target, 'source': source, 'actual': actual})
        preview = {'label': label, 'email': actual['email'], 'source': source,
                   'cache_repaired': any((config.get('oauthAccount') or {}).get(k) != v for k, v in
                                         (('accountUuid', actual['account']), ('organizationUuid', actual['organization']), ('emailAddress', actual['email']))),
                   'signature': signature}
        if expected is None:
            return preview
        if expected != signature:
            raise ImportError('The login file or profile changed after verification. Verify the file again before importing.')
        account, org = profile['account'], profile['organization']
        credential.update(subscriptionType='max' if account.get('has_claude_max') else 'pro' if account.get('has_claude_pro') else 'free',
                          rateLimitTier=org.get('rate_limit_tier'))
        payload = json.dumps({**old, 'claudeAiOauth': credential}, separators=(',', ':')).encode()
        # Recheck after network/Keychain prompts, before either write. Locks also
        # exclude our background refresh and Desktop association transactions.
        if keychain.read() != before or (config_path.read_bytes() if config_path.exists() else None) != raw_config:
            raise ImportError('The CLI login changed while verifying. Retry the import.')
        keychain.write(payload)
        try:
            if keychain.read() != payload:
                raise ImportError('Keychain did not retain the imported login.')
            writer(config_path, {**config, 'oauthAccount': cached_identity(profile)})
        except BaseException:
            try:
                keychain.write(before)
            except Exception:
                raise ImportError('The login could not be fully saved or rolled back. Retry this import to reconcile the CLI identity.') from None
            raise ImportError('The identity cache could not be saved. The previous Keychain login was restored; retry the import.') from None
        return preview


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('label')
    parser.add_argument('path')
    parser.add_argument('--apply', metavar='PREVIEW_SIGNATURE')
    args = parser.parse_args(argv)
    try:
        result = import_login(Path.home(), args.label, normalize_path(args.path), args.apply)
        if args.apply:
            print(f'CLI login imported for profile {result["label"]}: {result["email"]}.')
            print('Cached CLI identity synchronized with the verified account.')
        else:
            print(json.dumps(result))
        return 0
    except (RuntimeError, OSError, ValueError, TypeError, AttributeError):
        error = sys.exc_info()[1]
        print(str(error) if isinstance(error, ImportError) else 'Import stopped. Check the file, profile and Keychain permissions; then retry.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    import runtime
    runtime.configure()
    runtime.run_main(main)
