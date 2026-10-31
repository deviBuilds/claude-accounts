"""Interactive Claude accounts. Uses the existing transactional profile helper."""
import argparse
import curses
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from cli_refresh import RefreshWorker, environment as profile_environment
import account_usage
import rotation
import runtime
import setup_options
import desktop_profiles as dp

HELPER = Path(__file__).with_name('desktop-sync.py')
ONBOARD = Path(__file__).with_name('onboarding.py')


def clean(value):
    return ''.join(c for c in str(value) if c.isprintable())


def helper_args(action, label, preview=False):
    if action in ('switch', 'save', 'login'):
        args = ['--' + action, label]
    elif action == 'sync':
        args = []
    elif action == 'recover':
        args = ['--recover']
    else:
        raise ValueError('Unknown helper action')
    return runtime.command(HELPER, *args, *(['--dry-run'] if preview else []))


def cli_environment(label, environ=None, home=None):
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}', label):
        raise ValueError('Invalid account label')
    source = os.environ if environ is None else environ
    env = profile_environment(label, home or Path.home(), source)
    # Ordinary --cli launches retain the user's customization preference.
    if 'CLAUDE_CODE_SAFE_MODE' in source:
        env['CLAUDE_CODE_SAFE_MODE'] = source['CLAUDE_CODE_SAFE_MODE']
    else:
        env.pop('CLAUDE_CODE_SAFE_MODE', None)
    return env


def disabled_reason(action, account, state):
    if state.get('recovery_pending') and action in ('switch', 'save', 'login', 'sync'):
        return 'An interrupted switch needs recovery first. Open Tools and select Recover interrupted switch.'
    if action == 'recover' and not state.get('recovery_pending'):
        return 'There is no interrupted switch to recover.'
    if action in ('switch', 'save', 'login', 'cli') and not account:
        return 'Wait for accounts to load, then select an account.'
    if action == 'switch' and account.get('identity_drift'):
        return 'CLI and Desktop logins differ. Resolve the mismatch before switching this profile.'
    if action == 'switch' and account.get('desktop_active'):
        return 'Already selected in Desktop. No account switch is needed.'
    if action == 'switch' and not account.get('desktop_saved'):
        return "Set up and save this account's Desktop login first."
    if action == 'login' and account.get('desktop_saved'):
        return 'This Desktop profile is already saved. Choose Switch Desktop to this account.'
    if action in ('save', 'login') and account.get('cli_state') in ('expired', 'missing', 'unverified', 'refreshing', 'refresh_failed', 'sign_in_required'):
        return 'Verify this CLI login first: choose Open CLI, use /exit to return, then retry.'
    return None


class Worker:
    """Subprocess output is queued so the terminal stays responsive during work."""
    def __init__(self, command, status=False):
        self.events = queue.Queue()
        self.process = None
        self.status = status
        self.thread = threading.Thread(target=self.run, args=(command,), daemon=True)
        self.thread.start()

    def run(self, command):
        try:
            self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            text=True, start_new_session=True,
                                            env={**os.environ, 'PYTHONUNBUFFERED': '1'})
            lines = []
            with self.process.stdout as output:
                for line in output:
                    line = line.rstrip('\n')
                    if self.status:
                        lines.append(line)
                    else:
                        self.events.put(('line', clean(line)))
            code = self.process.wait()
            if self.status:
                try:
                    data = json.loads('\n'.join(lines)) if code == 0 else None
                    if not isinstance(data, dict) or not isinstance(data.get('accounts'), list):
                        raise ValueError('Account status could not be loaded')
                    self.events.put(('status', data))
                except (ValueError, TypeError):
                    self.events.put(('line', 'Status check failed. ' + clean(' '.join(lines))[:500]))
            self.events.put(('done', code))
        except OSError as error:
            self.events.put(('line', clean(error)))
            self.events.put(('done', 1))


class App:
    def __init__(self, screen, demo=False, onboard=False):
        self.screen, self.demo = screen, demo
        self.onboard = onboard
        self.components = list(setup_options.CHOICES) if demo else setup_options.load()
        self.state = {'accounts': []}
        self.page, self.cursor, self.label = 'home', 0, None
        self.wizard, self.input_text = None, ''
        self.worker, self.job, self.after_status = None, None, None
        self.deferred_action = None
        self.logs, self.modal, self.scroll = [], None, 0
        self.running = True
        self.notice = ''
        self.desktop_seen = None
        self.desktop_checked = self.last_refresh = 0
        self.cli_worker = None
        self.cli_results = {}
        self.refresh_pending = False
        self.pending_cli_check = None
        screen.keypad(True)
        screen.timeout(100)
        try:
            curses.curs_set(0)
            curses.set_escdelay(25)
        except (curses.error, AttributeError):
            pass
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            # Color roles include background fills, so selection is a complete card.
            palette = {
                1: (81, -1), 2: (114, -1), 3: (221, -1), 4: (246, -1),
                5: (252, -1), 6: (240, -1), 7: (252, 235),
                8: (255, 24), 9: (153, 24), 10: (87, 24),
                11: (183, -1), 12: (210, -1), 13: (246, 235),
                14: (157, 22), 15: (229, 58), 16: (159, 24),
                17: (217, 52), 18: (81, 235), 19: (183, 235),
                20: (240, -1), 21: (67, 235), 22: (221, 235),
                23: (232, 232), 24: (16, 81),
                25: (114, 235), 26: (210, 235), 27: (114, 24), 28: (221, 24), 29: (210, 24),
            }
            fallback = {
                1: (6, -1), 2: (2, -1), 3: (3, -1), 4: (7, -1),
                5: (7, -1), 6: (6, -1), 7: (7, -1),
                8: (7, 4), 9: (7, 4), 10: (6, 4),
                11: (5, -1), 12: (1, -1), 13: (7, -1),
                14: (0, 2), 15: (0, 3), 16: (7, 4),
                17: (7, 1), 18: (6, -1), 19: (5, -1),
                20: (7, -1), 21: (6, -1), 22: (3, -1),
                23: (0, 0), 24: (0, 6),
                25: (2, -1), 26: (1, -1), 27: (2, 4), 28: (3, 4), 29: (1, 4),
            }
            for n, (fg, bg) in (palette if curses.COLORS >= 256 else fallback).items():
                curses.init_pair(n, fg, bg)
        self.refresh()

    @property
    def account(self):
        return next((a for a in self.state.get('accounts', []) if a['label'] == self.label), None)

    def color(self, number):
        return curses.color_pair(number) if curses.has_colors() else 0

    def put(self, y, x, text, attr=0, width=None):
        height, columns = self.screen.getmaxyx()
        if not (0 <= y < height and 0 <= x < columns):
            return
        if self.modal and not getattr(self, '_modal_paint', False):
            attr = self.color(20) | (0 if curses.has_colors() else curses.A_DIM)
        try:
            self.screen.addnstr(y, x, clean(text), max(0, min(columns-x-1, width or columns)), attr)
        except curses.error:
            pass

    def goto(self, page):
        self.page, self.cursor = page, 0
        self.notice = ''

    def refresh(self, after=None):
        if self.worker:
            return
        self.after_status = after
        if self.demo:
            self.state = {'desktop_running': False, 'desktop_detail': 'Local login verified', 'recovery_pending': False,
                          'accounts': [
                              {'label': 'a', 'email': None, 'cli_state': 'expired', 'cli_detail': 'Open CLI to refresh.', 'desktop_saved': True, 'desktop_active': True},
                              {'label': 'b', 'email': 'ankit@example.test', 'cli_state': 'ready', 'cli_detail': 'Account verified.', 'desktop_saved': False, 'pending': True},
                              {'label': 'c', 'email': 'hello@example.test', 'cli_state': 'ready', 'cli_detail': 'Account verified.', 'desktop_saved': True, 'desktop_active': False}]}
            if after:
                self.status_ready()
            return
        self.desktop_seen = self.desktop_signature()
        self.last_refresh = time.monotonic()
        self.worker = Worker(runtime.command(HELPER, '--status-json'), status=True)
        self.job = 'status'
        self.status_received = False

    def desktop_signature(self):
        root = Path.home() / 'Library/Application Support/Claude'
        signature = []
        for name in ('config.json', 'Cookies', 'Cookies-wal', 'Cookies-journal'):
            try:
                stat = (root / name).stat()
                signature.append((stat.st_ino, stat.st_mtime_ns, stat.st_size))
            except OSError:
                signature.append(None)
        return tuple(signature)

    def watch_desktop(self):
        if self.demo or self.modal or (self.page != 'desktop_change' and (self.page != 'wizard' or self.wizard['stage'] != 'finish')):
            return
        now = time.monotonic()
        if now - self.desktop_checked < 1.5 or now - self.last_refresh < 3:
            return
        self.desktop_checked = now
        # Observe local file changes; avoid repeatedly querying account APIs
        # while the user is just looking at an unchanged sign-in screen.
        if self.desktop_signature() != self.desktop_seen:
            self.refresh()

    def advance_cli_setup(self):
        row = self.account or {}
        if self.page != 'wizard' or self.wizard['stage'] != 'cli' or row.get('cli_state') != 'ready':
            return False
        if 'desktop' not in self.components:
            stage = 'complete'
        elif row.get('desktop_active') and row.get('desktop_saved') and not row.get('pending') and not row.get('identity_drift'):
            stage = 'complete'
        elif row.get('pending') or row.get('cli_desktop_matches', row.get('desktop_active')):
            stage = 'finish'
        else:
            stage = 'desktop'
        self.wizard['stage'], self.cursor = stage, 0
        self.notice = 'CLI login verified: ' + str(row.get('email') or self.label)
        self.pending_cli_check = None
        # A login action queued during status loading no longer applies.
        self.deferred_action = None
        return True

    def status_ready(self):
        if self.onboard:
            self.onboard = False
            rows = self.state.get('accounts', [])
            if len(rows) == 1 and not rows[0].get('desktop_saved') and not self.state.get('recovery_pending'):
                self.label = rows[0]['label']
                self.begin_setup()
        after, self.after_status = self.after_status, None
        if after == 'verify_cli':
            row = self.account
            if not self.demo and row and row.get('cli_state') in ('expired', 'refreshing'):
                self.pending_cli_check = self.label
                if not self.cli_worker:
                    self.start_cli_refresh(row)
                return
            if row and row['cli_state'] == 'ready':
                self.advance_cli_setup()
            else:
                self.message('CLI sign-in needs attention', [row.get('cli_detail', '') if row else 'Account status is unavailable.',
                                                          'Retry sign-in or check the existing login to continue.'])
        elif self.page == 'wizard' and self.wizard['stage'] in ('desktop', 'finish'):
            row = self.account or {}
            if row.get('cli_desktop_matches', row.get('desktop_active')) and not row.get('identity_drift'):
                self.wizard['stage'] = 'complete' if row.get('desktop_saved') and not row.get('pending') else 'finish'
                self.cursor = 0
                self.notice = ('Desktop login is already saved.' if self.wizard['stage'] == 'complete'
                               else 'Desktop sign-in detected. Save this login to finish setup.')
        self.advance_cli_setup()

    def apply_refresh_results(self):
        for row in self.state.get('accounts', []):
            result = self.cli_results.get((row['label'], row.get('refresh_id')))
            if result and row.get('cli_state') != 'ready':
                row.update(cli_state=result['cli_state'], cli_detail=result['cli_detail'])
            if self.cli_worker and row['label'] == self.cli_worker.label:
                row.update(cli_state='refreshing', cli_detail='Refreshing CLI login in the background. You can keep using this menu.')

    def poll_cli_refresh(self):
        if self.cli_worker:
            try:
                result = self.cli_worker.events.get_nowait()
            except queue.Empty:
                return
            worker, self.cli_worker = self.cli_worker, None
            self.cli_results[(worker.label, worker.generation)] = result
            if result.get('refresh_id'):
                self.cli_results[(worker.label, result['refresh_id'])] = result
            for row in self.state.get('accounts', []):
                if row['label'] == worker.label and row.get('refresh_id') in (worker.generation, result.get('before_id'), result.get('refresh_id')):
                    row.update({k: v for k, v in result.items() if k in ('cli_state', 'cli_detail', 'refresh_id', 'expires_at', 'email')})
                    if result['cli_state'] == 'ready':
                        row.update(email_source='verified with Anthropic', cli_email=result['email'], cli_account=result['account'],
                                   identity_drift=bool(row.get('desktop_account') and row['desktop_account'] != result['account']),
                                   cli_desktop_matches=result['account'] == self.state.get('active_account'),
                                   desktop_active=(row.get('desktop_account') or result['account']) == self.state.get('active_account'))
            if result['cli_state'] == 'ready':
                self.notice = f'CLI login refreshed for account {worker.label}.'
                self.refresh_pending = True
            else:
                self.notice = f'Account {worker.label}: ' + result['cli_detail']
            if self.pending_cli_check == worker.label:
                self.pending_cli_check = None
                if self.page == 'wizard' and self.label == worker.label:
                    if result['cli_state'] != 'ready':
                        self.message('CLI sign-in needs attention', [result['cli_detail']])
            self.advance_cli_setup()
        if self.demo or self.worker or self.modal or self.cli_worker:
            return
        if self.refresh_pending:
            self.refresh_pending = False
            self.refresh()
            return
        for row in self.state.get('accounts', []):
            expires = row.get('expires_at')
            if row.get('cli_state') == 'ready' and isinstance(expires, (int, float)) and expires <= time.time():
                row['cli_state'] = 'expired'
            if row.get('cli_state') == 'expired' and row.get('refresh_id'):
                self.start_cli_refresh(row)
                return

    def start_cli_refresh(self, row, force=False):
        if self.demo or self.cli_worker:
            return
        self.cli_worker = RefreshWorker(row['label'], row.get('refresh_id'), force=force)
        row.update(cli_state='refreshing', cli_detail='Refreshing CLI login in the background. You can keep using this menu.')

    def poll(self):
        self.poll_cli_refresh()
        if not self.worker:
            if self.cli_worker:
                return
            self.watch_desktop()
            if not self.demo and not self.worker and not self.modal and self.last_refresh > 0 and time.monotonic()-self.last_refresh >= account_usage.TTL:
                self.refresh()
            return
        while True:
            try:
                event, value = self.worker.events.get_nowait()
            except queue.Empty:
                return
            if event == 'line':
                self.logs = (self.logs + [value])[-200:]
            elif event == 'status':
                self.state, self.status_received = value, True
                self.apply_refresh_results()
            elif event == 'done':
                job = self.job
                self.worker, self.job = None, None
                if job == 'status':
                    if self.status_received:
                        self.status_ready()
                        if self.state.get('errors'):
                            self.notice = '; '.join(self.state['errors'])
                    else:
                        self.after_status = None
                        self.message('Could not refresh accounts', self.logs[-5:] or ['Try Refresh accounts again.'])
                    deferred, self.deferred_action = self.deferred_action, None
                    if deferred and not self.modal:
                        page, label, stage, action = deferred
                        if (self.page, self.label, (self.wizard or {}).get('stage')) == (page, label, stage):
                            self.dispatch(action)
                elif value:
                    if job in ('save', 'onboard:finish', 'onboard:finish-desktop', 'manage:finish-desktop-change') and any('DESKTOP_BUSY:' in line for line in self.logs):
                        action = job.removeprefix('onboard:')
                        self.desktop_save_blocked(action)
                        return
                    self.message('Action needs attention', self.logs)
                    if job.startswith('onboard:'):
                        if job in ('onboard:finish', 'onboard:finish-desktop'):
                            self.wizard['stage'] = 'desktop'
                            self.cursor = 0
                        self.refresh()
                elif job.startswith('rotation:'):
                    self.notice = ' '.join(self.logs[-2:]) or 'Rotation check complete.'
                    self.refresh()
                elif job.startswith('manage:'):
                    action = job.split(':', 1)[1]
                    if action == 'reset-cli-setup':
                        self.launch_cli(self.label, authenticate=True, email_hint=getattr(self, 'login_hint', ''))
                        return
                    self.goto('desktop_change' if action in ('begin-desktop-change', 'desktop-to-cli') else
                              'home' if action in ('remove', 'restore') else 'account')
                    self.notice = ' '.join(self.logs[-2:])
                    self.refresh()
                elif job.startswith('onboard:'):
                    action = job.split(':')[1]
                    self.wizard['stage'] = {'create': 'cli', 'prepare': 'finish', 'finish': 'complete', 'create-desktop': 'desktop', 'prepare-desktop': 'finish', 'finish-desktop': 'complete'}[action]
                    self.cursor = 0
                    self.notice = 'Profile created.' if action == 'create' else ''
                    self.refresh()
                else:
                    self.modal = ('result', None, 'Finished', self.logs[:])
                    self.scroll = 0
                return

    def message(self, title, lines):
        self.modal, self.scroll = ('message', None, title, lines), 0

    def confirm(self, action, title, lines):
        self.modal, self.scroll = ('confirm', action, title, lines), 0

    def check_desktop_state(self):
        state = dp.desktop_state()
        self.state.update(desktop_running=state['running'], desktop_background=state['background'])
        return state

    def desktop_save_blocked(self, action):
        try:
            self.check_desktop_state()
        except (RuntimeError, OSError):
            self.state.update(desktop_running=None, desktop_background=None)
        reason = next((line.split('DESKTOP_BUSY:', 1)[1].strip()
                       for line in reversed(self.logs) if 'DESKTOP_BUSY:' in line),
                      'Some Desktop tasks could not be stopped.')
        self.confirm('retry_save:' + action, 'Desktop could not finish stopping',
                     [reason,
                      'Retry to stop the remaining Desktop tasks and save the login.',
                      'Your login and setup progress are kept. No profile files were changed.'])

    def request_desktop_save(self, action):
        if self.demo:
            self.start(action)
            return
        try:
            state = self.check_desktop_state()
        except (RuntimeError, OSError) as error:
            self.message('Cannot check Desktop', [str(error), 'Retry saving once the process check is available.'])
            return
        if state['running']:
            self.confirm('close_save:' + action, 'Close Desktop and save?',
                         ['Claude Desktop is open. Saving its login requires closing it first.',
                          'Continue to quit Desktop, stop any leftover tasks, save the login, and reopen it.',
                          'Running Desktop sessions will pause.'])
        elif state['background']:
            self.confirm('stop_save:' + action, 'Stop background tasks and save?',
                         ['The Desktop app is closed, but some of its tasks are still running.',
                          'Continue to stop those tasks automatically and save the login. '
                          'Tasks that do not exit normally will be force-stopped.',
                          'Desktop stays closed. Standalone CLI sessions are left running.'])
        else:
            self.start(action)

    def rotation_status(self):
        return rotation.Rotation().status() if not self.demo else {'enabled': False, 'accounts': []}

    def rotation_health(self, status):
        if not status['enabled']:
            return 'DISABLED', 13
        if status.get('state') == 'error':
            return 'ERROR', 17
        if not status.get('checked_at'):
            return 'AWAITING FIRST CHECK', 15
        if status.get('stale'):
            return 'MONITOR DELAYED', 15
        return {'ready': ('RUNNING', 14), 'waiting': ('WAITING FOR ACCOUNT', 15),
                'unknown': ('CHECK UNAVAILABLE', 15)}.get(status.get('state'), ('CHECKING', 16))

    def rotation_accounts(self):
        now = time.time()
        return [row for row in self.state.get('accounts', [])
                if row.get('cli_state') == 'ready'
                and (not isinstance(row.get('expires_at'), (int, float)) or row['expires_at'] > now)]

    def prune_rotation_selection(self):
        eligible = {row['label'] for row in self.rotation_accounts()}
        self.rotation_selected = [label for label in getattr(self, 'rotation_selected', []) if label in eligible]

    def items(self):
        if self.page == 'home':
            rows = []
            for a in self.state.get('accounts', []):
                if self.state.get('first_run') and a['label'] == 'a':
                    continue
                desktop = 'Desktop active' if a.get('desktop_active') else 'Desktop saved' if a.get('desktop_saved') else 'Desktop not set up'
                if a.get('pending'):
                    desktop = 'Desktop sign-in in progress'
                rows.append(('account:' + a['label'], f'{a["label"]}  {a.get("email") or "Identity not verified"}',
                             f'CLI {a["cli_state"]}  /  {desktop}'))
            if self.state.get('first_run'):
                rows.insert(0, ('first_account', 'Set up your first account', 'Connect your selected Claude apps'))
            return rows + [('add', '+ Add new account', 'Set up the Claude apps you selected'),
                           ('refresh', 'Refresh accounts', 'Check current logins'),
                           ('tools', 'Tools', 'Sync history or recover an interrupted switch'),
                           ('quit', 'Quit', '')]
        if self.page == 'rotation':
            self.prune_rotation_selection()
            selected = self.rotation_selected
            rows = [('rotate_pick:' + a['label'], ('[x] ' if a['label'] in selected else '[ ] ') + a['label'] + '  ' + str(a.get('cli_email') or a.get('email') or 'Sign-in needed'),
                     'Selected. Space to remove; save your changes to apply.' if a['label'] in selected else 'Not selected. Space to include this account.')
                    for a in self.rotation_accounts()]
            status = self.rotation_status()
            actions = []
            if len(selected) >= 2 and (not status['enabled'] or selected != [a['label'] for a in status.get('accounts', [])]):
                actions.append(('rotate_enable', 'Save account selection' if status['enabled'] else 'Enable auto rotation',
                                'Apply the selected accounts to the background monitor'))
            if status['enabled']:
                actions += [('rotate_check', 'Check quotas now', 'Run a fresh rotation check'),
                            ('rotate_disable', 'Disable auto rotation', 'Stop checks; keep shared sessions')]
            else:
                actions.append(('refresh', 'Refresh CLI logins', 'Check sign-ins; only valid CLI sessions appear here'))
            return rows + actions + [('rotate_help', 'T3Code setup & details', 'Shared directory, quota handling and recovery'),
                                     ('back', 'Back to Tools', '')]
        if self.page == 'account':
            a = self.account or {}
            desktop = [('switch', 'Switch Desktop to this account', 'Save the current account automatically, then restore this login')] if a.get('desktop_saved') and not a.get('desktop_active') and not a.get('identity_drift') else []
            setup = []
            if not a.get('desktop_saved'):
                if a.get('pending'):
                    setup = [('setup', 'Finish Desktop sign-in', 'Continue the sign-in you started, then verify and save it')]
                elif not a.get('desktop_active'):
                    detail = ('Sign in once in Desktop and save the login for switching'
                              if a.get('cli_state') == 'ready' else 'First verify your CLI login, then sign in to Desktop' if 'cli' in self.components else 'Sign in to Desktop and save this account')
                    setup = [('setup', 'Set up Desktop login', detail)]
            retry = [('retry_refresh', 'Retry background refresh', 'Try renewing this CLI login without opening the interactive CLI')] if a.get('cli_state') in ('refresh_failed', 'sign_in_required') else []
            repair = [('reconcile_menu', 'Resolve CLI/Desktop mismatch', 'Choose which login to keep and review the repair')] if a.get('identity_drift') else []
            resume = [('desktop_change', 'Resume Desktop login change', 'Finish saving the new Desktop sign-in')] if a.get('desktop_changing') else []
            rows = repair + resume + desktop + setup + retry + [('usage', 'View usage limits', 'All usage windows, reset dates and last update time'),
                              ('cli', 'Open CLI', 'Open an interactive Claude session; /exit returns here'),
                              ('auth', 'Refresh CLI sign-in', 'Open the official browser sign-in flow'),
                              ('save', 'Save current Desktop login', 'Verify it matches this CLI account first' if 'cli' in self.components else 'Verify and save the signed-in Desktop account'),
                              ('manage', 'Manage profile', 'Change logins, reconcile identities, or remove this profile'),
                              ('back', 'Back to accounts', '')]
            if 'cli' not in self.components:
                rows = [r for r in rows if r[0] not in ('cli', 'auth', 'retry_refresh', 'usage')]
            if 'desktop' not in self.components:
                rows = [r for r in rows if r[0] not in ('switch', 'setup', 'save')]
            if a.get('identity_drift') or a.get('desktop_changing'):
                rows = [r for r in rows if r[0] != 'save']
            return rows
        if self.page == 'manage':
            rows = []
            if 'cli' in self.components:
                rows.append(('change_cli', 'Change CLI login', 'Choose a different account in the official sign-in flow'))
            if 'desktop' in self.components:
                rows.append(('change_desktop', 'Change Desktop login', 'Preserve the old login and open a clean Desktop sign-in'))
            if (self.account or {}).get('identity_drift'):
                rows.append(('reconcile_menu', 'Reconcile CLI and Desktop', 'Review both identities and choose which to keep'))
            if (self.account or {}).get('pending'):
                rows.append(('cancel_desktop', 'Cancel pending Desktop setup', 'Keep saved logins and current app data'))
            return rows + [('remove_profile', 'Remove profile from manager', 'Retain logins and history; restore later from Tools'),
                           ('back', 'Back to profile', '')]
        if self.page == 'reconcile':
            plan = self.state.get('reconcile_plan', {})
            rows = []
            if any(c['label'] == self.label for c in plan.get('changes', [])):
                rows.append(('repair_bindings', 'Match saved Desktop logins to CLI accounts', 'Reuse verified saved logins; preview every affected profile'))
            return rows + [('desktop_to_cli', 'Keep CLI account; sign Desktop into it', 'Preserve the old Desktop login, then sign in to the CLI account'),
                           ('cli_to_desktop', 'Keep Desktop account; change CLI login', 'Sign CLI into the account saved for Desktop'),
                           ('refresh', 'Refresh identities', ''), ('back', 'Back to profile', '')]
        if self.page == 'desktop_change':
            return [('save_desktop_change', 'Save new Desktop login', 'Verify and save the new login; retain the previous snapshot'),
                    ('resume_desktop_change', 'Open Desktop to continue sign-in', ''),
                    ('cancel_desktop', 'Cancel Desktop login change', 'Keep saved logins and current app data'),
                    ('back', 'Back to profile', '')]
        if self.page == 'removed':
            return [('restore:' + label, 'Restore ' + label, 'Show this profile with its retained logins')
                    for label in self.state.get('removed_profiles', [])] + [('back', 'Back to tools', '')]
        if self.page == 'tools':
            rows = [('sync', 'Sync local Code session history', 'Copy local session entries across accounts')] if set(setup_options.CHOICES).issubset(self.components) else []
            if self.state.get('recovery_pending'):
                rows.insert(0, ('recover', 'Recover interrupted switch', 'Restore the original Desktop profile'))
            if 'cli' in self.components:
                rows.insert(0, ('rotation', 'Auto rotation', 'Manage background CLI account rotation'))
            return rows + [('removed', 'Removed profiles', 'Restore a profile removed from the manager'), ('back', 'Back to accounts', '')]
        if self.page == 'wizard':
            stage = self.wizard['stage']
            if stage == 'cli':
                rows = [('wizard_cli', 'Open CLI to sign in', 'Use /login in Claude, then /exit to return and verify'),
                        ('wizard_auth', 'Browser sign-in (alternative)', 'Run Claude auth login and return here when finished'),
                        ('verify_cli', 'Check existing CLI login', 'Continue if this profile is already signed in')]
                if self.account and self.account.get('cli_state') in ('expired', 'refreshing'):
                    rows = [('verify_cli', 'Check login status', 'Your existing CLI login is being renewed automatically')]
            elif stage == 'desktop':
                rows = [('prepare', 'Open Desktop sign-in', 'Preserve the current login and prepare this account')]
            elif stage == 'finish':
                if self.account and self.account.get('desktop_active'):
                    rows = [('finish', 'Save Desktop login and finish', 'Save the detected login for future account switching')]
                else:
                    rows = [('finish', 'Verify and finish setup', 'Verify and save the signed-in Desktop account' if 'cli' not in self.components else 'Check Desktop matches the CLI, then save its login'),
                            ('prepare', 'Open Desktop sign-in', 'Resume sign-in, or preserve the wrong account and prepare a fresh login')]
            elif stage == 'complete':
                return [('done', 'Done — back to accounts', '')]
            else:
                return []
            return rows + [('pause', 'Pause setup and return to accounts', 'Your progress is kept; select this account to resume')]
        return []

    def begin_setup(self, new=False):
        if self.state.get('recovery_pending'):
            self.message('Recovery required', ['Open Tools and recover the interrupted Desktop switch first.'])
            return
        if new:
            self.input_text = ''
            self.wizard = {'stage': 'name'}
        else:
            a = self.account or {}
            if 'cli' not in self.components:
                stage = 'finish' if a.get('pending') else 'desktop'
            elif 'desktop' not in self.components:
                stage = 'complete' if a.get('cli_state') == 'ready' else 'cli'
            else:
                stage = 'finish' if a.get('pending') and a.get('cli_state') == 'ready' else 'desktop' if a.get('cli_state') == 'ready' else 'cli'
            self.wizard = {'stage': stage}
        self.goto('wizard')

    def dispatch(self, action):
        if self.worker:
            return
        if self.cli_worker and self.label == self.cli_worker.label and action in ('cli', 'auth', 'wizard_auth', 'wizard_cli', 'setup', 'save', 'prepare', 'finish', 'verify_cli', 'retry_refresh', 'change_cli', 'cli_to_desktop', 'change_desktop', 'desktop_to_cli', 'repair_bindings', 'remove_profile'):
            self.notice = 'This CLI login is refreshing in the background. Please wait before using its login actions.'
            return
        if action == 'rotation':
            status = self.rotation_status()
            self.rotation_selected = [a['label'] for a in status['accounts']]
            self.prune_rotation_selection()
            self.goto('rotation')
            return
        if action.startswith('rotate_pick:'):
            label = action.split(':', 1)[1]
            self.prune_rotation_selection()
            if label not in {row['label'] for row in self.rotation_accounts()}:
                return
            if label in self.rotation_selected:
                self.rotation_selected.remove(label)
            else:
                self.rotation_selected.append(label)
            return
        if action == 'rotate_enable':
            self.prune_rotation_selection()
            self.confirm('rotation:enable', 'Enable automatic account rotation?',
                         ['Selected profiles: ' + ', '.join(self.rotation_selected),
                          'Conversations in ~/.claude-rotate are shared across these accounts.',
                          'A LaunchAgent checks every minute, refreshes expired logins, and changes accounts when a reported limit is exhausted.',
                          'Existing requests are not restarted or replayed. A new request can use the next account.',
                          'Refresh tokens remain in original profiles. Select at least two distinct signed-in accounts.'])
            return
        if action == 'rotate_disable':
            self.start('rotation:disable')
            return
        if action == 'rotate_check':
            self.start('rotation:tick')
            return
        if action == 'rotate_help':
            self.message('Auto rotation / T3Code', [
                'T3Code: Binary path = claude; CLAUDE_CONFIG_DIR = ' + str(Path.home()/'.claude-rotate'),
                'Terminal: use claude-rotate. Do not use /login or /logout inside the shared profile.',
                'Checks run every 60 seconds, subject to server backoff. Rotation keeps the active account until a reported limit is exhausted or renewal fails.',
                'Any reported model limit conservatively makes an account ineligible.',
                'The tested CLI reloads shared access tokens on its next API request. In-flight requests are not killed or replayed.',
                'A turn already waiting for quota may need Stop then a new message.',
                'If all accounts are unavailable, rotation waits. Original profiles and Desktop logins are unchanged.'])
            return
        if action.startswith('account:'):
            self.label = action.split(':', 1)[1]
            self.goto('account')
        elif action in ('manage', 'reconcile_menu', 'desktop_change', 'removed'):
            self.goto({'reconcile_menu': 'reconcile'}.get(action, action))
        elif action == 'back' and self.page in ('manage', 'reconcile', 'desktop_change', 'removed', 'rotation'):
            self.goto('tools' if self.page in ('removed', 'rotation') else 'account')
        elif action.startswith('restore:'):
            self.label = action.split(':', 1)[1]
            self.confirm('manage:restore', 'Restore profile ' + self.label + '?', ['Show the retained CLI and Desktop logins in the manager again.'])
        elif action in ('change_cli', 'cli_to_desktop'):
            self.login_hint = ((self.account or {}).get('desktop_email') or '') if action == 'cli_to_desktop' else ''
            self.confirm('manage:reset-cli-setup', 'Change CLI login for ' + self.label + '?',
                         ['Choose ' + (self.login_hint or 'the new account') + ' in the official sign-in flow.',
                          'This replaces the CLI login for this profile. Its saved Desktop login stays unchanged.',
                          'After sign-in, the manager checks the identity and reports any mismatch.'])
        elif action in ('change_desktop', 'desktop_to_cli'):
            op = 'desktop-to-cli' if action == 'desktop_to_cli' else 'begin-desktop-change'
            target = (self.account or {}).get('cli_email') if action == 'desktop_to_cli' else None
            self.confirm('manage:' + op, 'Change Desktop login for ' + self.label + '?',
                         ['Preserve the current Desktop login, then open a clean sign-in.',
                          'Sign into ' + target + '.' if target else 'Choose the new Desktop account in the browser.',
                          'Desktop will quit and reopen. Running Desktop sessions will pause.',
                          'The old saved login is retained until the new login is verified and saved.'])
        elif action == 'repair_bindings':
            plan = self.state.get('reconcile_plan', {})
            if not any(c['label'] == self.label for c in plan.get('changes', [])):
                self.message('No saved match', ['Refresh identities or choose a sign-in option.'])
                return
            lines = [f"{c['label']}: Desktop {c['before'].get('email') or c['before']['account']} → {c['after'].get('email') or c['after']['account']}"
                     for c in plan['changes']]
            self.confirm('manage:reconcile:' + plan['signature'], 'Match Desktop to current CLI logins?',
                         lines + ['These saved associations change together. CLI logins and the running Desktop app stay unchanged.'])
        elif action == 'remove_profile':
            self.confirm('manage:remove', 'Remove profile ' + self.label + '?',
                         ['Remove this profile from the manager’s list.',
                          'CLI configuration, Keychain logins, saved Desktop data and shared history are retained. Neither app is signed out.',
                          'Restore it from Tools → Removed profiles. The label stays reserved.'])
        elif action == 'save_desktop_change':
            self.request_desktop_save('manage:finish-desktop-change')
        elif action == 'resume_desktop_change':
            self.start('manage:begin-desktop-change')
        elif action == 'cancel_desktop':
            self.confirm('manage:cancel-desktop-change', 'Cancel Desktop setup?',
                         ['Keep the saved login and leave the current Desktop app unchanged. You can switch to a saved login afterward.'])
        elif action == 'first_account':
            if 'cli' in self.components and not self.state.get('cli_available', True):
                self.message('Install Claude Code first', ['Install Claude Code from https://code.claude.com/docs/en/setup',
                             'Then reopen this account manager. Python 3.10+ and macOS are required.'])
                return
            self.label = 'a'
            self.begin_setup()
        elif action == 'usage':
            row = self.account or {}
            self.message('Usage — ' + str(row.get('email') or self.label), account_usage.details(row.get('usage', {})))
        elif action == 'add':
            self.begin_setup(new=True)
        elif action == 'setup':
            self.begin_setup()
        elif action == 'tools':
            self.goto('tools')
        elif action in ('back', 'pause', 'done'):
            self.wizard = None
            self.goto('home')
            self.refresh()
        elif action == 'quit':
            self.running = False
        elif action == 'refresh':
            self.refresh()
        elif action == 'retry_refresh':
            if self.account:
                self.start_cli_refresh(self.account, force=True)
        elif action == 'verify_cli':
            self.refresh('verify_cli')
        elif action in ('cli', 'auth', 'wizard_auth', 'wizard_cli'):
            if action == 'cli':
                self.confirm(action, 'Open Claude CLI', ['Use /exit to return here after Claude refreshes its login.',
                                                      'This launch uses safe mode (custom hooks and plugins disabled).'])
            elif action == 'wizard_cli':
                self.launch_cli(self.label)
            else:
                self.launch_cli(self.label, authenticate=True)
        elif action in ('finish', 'finish-desktop'):
            self.request_desktop_save(action)
        elif action in ('prepare', 'prepare-desktop'):
            self.start_onboarding(action)
        elif action == 'save':
            reason = disabled_reason(action, self.account, self.state) if 'cli' in self.components else None
            if reason:
                self.message('Next step', [reason])
            else:
                self.request_desktop_save('save' if 'cli' in self.components else 'save_desktop')
        else:
            reason = disabled_reason(action, self.account, self.state)
            if reason:
                self.message('Next step', [reason])
                return
            description = {'switch': 'Save the current Desktop account, then restore this login.',
                           'save': 'Verify and save the current Desktop login under this account.',
                           'recover': 'Restore the original Desktop profile from the interrupted switch.',
                           'sync': 'Copy the newest local Code session entries across verified accounts.'}[action]
            lines = [description]
            if action != 'sync':
                lines += ['Desktop will quit and reopen. Running Desktop sessions will pause.']
            self.confirm(action, {'switch': 'Switch Desktop account?', 'save': 'Save Desktop login?',
                                  'recover': 'Recover Desktop profile?', 'sync': 'Sync local history?'}[action], lines)

    def start_onboarding(self, action):
        if 'cli' not in self.components and action in ('create', 'prepare', 'finish'):
            action += '-desktop'
        if self.demo:
            self.message('Demo mode', ['Setup actions are disabled. No accounts were changed.'])
            return
        self.logs = []
        self.worker = Worker(runtime.command(ONBOARD, action, self.label))
        self.job = 'onboard:' + action

    def start(self, action):
        if action.startswith(('close_save:', 'stop_save:', 'retry_save:')):
            action = action.split(':', 1)[1]
        if action.startswith('rotation:'):
            if self.demo:
                self.message('Demo mode', ['Rotation changes are disabled.'])
                return
            mode = action.split(':', 1)[1]
            args = ['--' + mode]
            if mode == 'enable':
                self.prune_rotation_selection()
                if len(self.rotation_selected) < 2:
                    self.message('Select accounts', ['Select at least two signed-in CLI profiles using Space.'])
                    return
                args += self.rotation_selected
            self.logs = []
            self.worker = Worker(runtime.command(Path(__file__).with_name('rotation.py'), *args))
            self.job = action
            return
        if action.startswith('manage:'):
            if self.demo:
                self.message('Demo mode', ['Profile changes are disabled.'])
                return
            if self.state.get('recovery_pending'):
                self.message('Recovery required', ['Recover the interrupted switch from Tools first.'])
                return
            parts = action.split(':', 2)
            args = [parts[1], self.label]
            if len(parts) == 3:
                args += ['--expected', parts[2]]
            self.logs = []
            self.worker = Worker(runtime.command(ONBOARD, *args))
            self.job = 'manage:' + parts[1]
            return
        if action in ('finish', 'finish-desktop'):
            self.start_onboarding(action)
            return
        if action == 'save_desktop':
            self.begin_setup()
            self.start_onboarding('finish')
            return
        if action in ('cli', 'wizard_cli'):
            self.launch_cli(self.label)
            return
        if self.demo:
            self.message('Demo mode', ['Actions are disabled. No profiles were changed.'])
            return
        self.logs = []
        self.worker = Worker(helper_args(action, self.label or 'a'))
        self.job = action

    def launch_cli(self, label, authenticate=False, email_hint=None):
        if self.demo:
            self.message('Demo mode', ['CLI launch is disabled in demo mode.'])
            return
        executable = shutil.which('claude')
        if not executable:
            self.message('CLI unavailable', ['Could not find claude on PATH.'])
            return
        command = [executable, 'auth', 'login', '--claudeai'] if authenticate else [executable, '--safe-mode']
        env = {**cli_environment(label), 'CLAUDE_CODE_SAFE_MODE': '1'}
        hint = ((self.account or {}).get('cli_email') or (self.account or {}).get('email')) if email_hint is None else email_hint
        if authenticate and hint and '@' in hint:
            command += ['--email', hint]
        command = runtime.interactive_command(command)
        result = 1
        self.logs = []
        curses.def_prog_mode()
        curses.endwin()
        try:
            print(f'Claude account {label}: ' + ('complete sign-in in your browser.' if authenticate else
                  'use /login to sign in, then /exit to return.' if self.page == 'wizard' else 'use /exit to return.'), flush=True)
            # An auth command must not inherit project-specific settings or hooks.
            if authenticate:
                with tempfile.TemporaryDirectory(prefix='claude-account-login-') as directory:
                    result = subprocess.run(command, cwd=directory, env=env, check=False).returncode
            else:
                result = subprocess.run(command, env=env, check=False).returncode
            if result and result != 130:
                # Keep the actual error visible before curses redraws over it.
                # Do not record a transcript: auth URLs and codes can be sensitive.
                print(f'\nClaude exited with status {result}. The CLI output above contains the error.', flush=True)
                try:
                    input('Press Enter to return to Claude Accounts… ')
                except EOFError:
                    pass
        except (OSError, KeyboardInterrupt) as error:
            self.logs = ['CLI returned: ' + clean(error)]
        finally:
            curses.reset_prog_mode()
            self.screen.clear()
            self.screen.keypad(True)
            self.screen.timeout(100)
            try:
                curses.curs_set(0)
            except curses.error:
                pass
        if result and authenticate:
            self.confirm('wizard_cli', 'Open CLI to finish sign-in?',
                         [f'Browser sign-in exited with status {result}. Your profile is kept.',
                          'Press Enter to open Claude here. Use /login, then /exit to return.',
                          'If a browser selected the wrong account, choose the intended account before approving.'] + self.logs[-2:])
        elif result:
            self.message('CLI stopped', [f'Claude exited with status {result}. You can retry from this screen.',
                         'Your profile and setup progress are kept.'] + self.logs[-2:])
        elif self.page == 'wizard':
            self.refresh('verify_cli')
        else:
            self.refresh()

    def header(self):
        if self.page == 'rotation':
            return 'Tools / Auto rotation', []
        if self.page == 'home':
            return 'Choose an account', ['One selection at a time. Press Enter to open it.']
        if self.page == 'removed':
            return 'Removed profiles', ['Logins and local history were retained. Restore a profile to show it again.']
        if self.page == 'tools':
            return 'Accounts / Tools', ['Maintenance actions']
        a = self.account or {}
        who = a.get('email') or self.label or 'new account'
        if self.page in ('manage', 'reconcile', 'desktop_change'):
            details = ['CLI: ' + str(a.get('cli_email') or 'Not verified'),
                       'Saved Desktop: ' + str(a.get('desktop_email') or a.get('desktop_account') or 'Not saved')]
            if self.page == 'desktop_change':
                details += ['Current Desktop: ' + str(self.state.get('active_email') or self.state.get('active_account') or 'Not signed in'),
                            'Complete the new sign-in in Desktop, then save it here. Previous saved logins are retained.']
            return {'manage': 'Manage profile / ', 'reconcile': 'Reconcile logins / ', 'desktop_change': 'Change Desktop login / '}[self.page] + str(self.label), details
        if self.page == 'account':
            details = ['CLI: ' + str(a.get('cli_email') or (a.get('email') if a.get('cli_state') == 'ready' else None) or 'Not verified'),
                       'Saved Desktop: ' + str(a.get('desktop_email') or a.get('desktop_account') or 'Not saved'),
                       a.get('cli_detail', 'Account status is loading.')]
            if a.get('desktop_active'):
                details.insert(0, 'Saved Desktop login is currently active.' if a.get('identity_drift') else 'Already selected in Desktop.')
            return ('Profile ' + str(self.label) + ' / logins differ' if a.get('identity_drift') else 'Accounts / ' + str(who)), details
        stage = self.wizard['stage']
        titles = {'name': 'Add account — choose a label', 'cli': 'Step 1 of 3 — CLI sign-in',
                  'desktop': 'Step 2 of 3 — Desktop sign-in', 'finish': 'Step 3 of 3 — Verify and save',
                  'complete': 'Account setup complete'}
        descriptions = {
            'name': ['Choose a profile label, for example work or zeno.',
                     'Existing CLI settings and local history are shared; the login is separate.'],
            'cli': [f'Profile: {self.label}. Sign in with the account you want to add.',
                    'Open CLI here, use /login, then /exit to return. The login is verified automatically.'],
            'desktop': [f'CLI verified: {who}. Use the same account in Desktop.',
                        'Continuing may quit and reopen Desktop, pausing its running sessions.',
                        'Your outgoing login is preserved. Complete browser sign-in, then return here.'],
            'finish': [f'Sign into {who} in Claude Desktop, then choose Verify and finish.',
                       'Only a matching CLI and Desktop account will be saved.',
                       'Saving briefly restarts Desktop and pauses running sessions.'],
            'complete': [f'CLI and Desktop are ready for {who}.',
                         f'CLI: claude-accounts --cli {self.label or "<label>"}',
                         'Next time, select this account and choose Switch Desktop.']}
        if 'cli' not in self.components:
            titles.update(desktop='Step 1 of 2 — Desktop sign-in', finish='Step 2 of 2 — Verify and save')
            descriptions['name'] = ['Choose a label for this Desktop account, such as work.', 'A CLI profile will not be created.']
            descriptions['desktop'] = [f'Open Desktop and sign into the account to save as {self.label}.', 'An existing login can be saved directly. Additional saved accounts switch through a clean sign-in.', 'CLI is not required. Usage percentages require a separate CLI login.']
            descriptions['finish'] = [f'Verify the account in Desktop, then save its login as {self.label}.', 'The encrypted Desktop login is checked before saving. Desktop briefly restarts.']
            descriptions['complete'] = [f'Desktop account {self.label} is saved.', 'Select it to switch Desktop next time. CLI was not required.']
        elif 'desktop' not in self.components:
            titles['cli'] = 'CLI sign-in'
            descriptions['complete'] = [f'CLI is ready for {who}.', f'Run: claude-accounts --cli {self.label}', 'Desktop was not part of this setup.']
        if stage == 'cli' and a.get('cli_state') in ('expired', 'refreshing'):
            titles['cli'] = 'Checking your existing CLI login'
            descriptions['cli'] = [f'Profile: {self.label}. An existing CLI login was detected.',
                                   'Renewing and verifying it automatically. No sign-in is needed while this runs.']
        if stage == 'finish' and a.get('desktop_active'):
            titles['finish'] = ('Step 2 of 2' if 'cli' not in self.components else 'Step 3 of 3') + ' — Save Desktop login'
            descriptions['finish'] = [f'Desktop is already signed in as {who}.',
                                      'Save this existing login to use it for future account switching.',
                                      'You will be asked to close Desktop before saving.' if self.state.get('desktop_running')
                                      else 'Saving will stop the remaining Desktop background tasks.' if self.state.get('desktop_background')
                                      else 'Desktop can stay closed while its login is saved.']
        return titles[stage], descriptions[stage]

    def fill(self, y, x, width, role):
        self.put(y, x, ' ' * width, self.color(role), width)

    def rule(self, y, title, width):
        self.put(y, 2, '─' * width, self.color(6), width)
        self.put(y, 4, ' ' + title + ' ', self.color(1) | curses.A_BOLD, width - 4)

    def badge(self, y, x, text, role, end):
        value = ' ' + text + ' '
        if x + len(value) <= end:
            self.put(y, x, value, self.color(role), len(value))
            return x + len(value) + 2
        return x

    def account_card(self, y, row, selected, width):
        base = 8 if selected else 7
        for line in (y, y+1):
            self.fill(line, 2, width, base)
            self.put(line, 2, '▌' if selected else '│', self.color(10 if selected else 18))
        label = '[' + row['label'] + ']'
        self.put(y, 5, label, self.color(10 if selected else 19) | curses.A_BOLD, min(len(label), width-7))
        x = 6 + len(label)
        self.put(y, x, ('CLI: ' + str(row.get('cli_email') or row.get('email'))) if row.get('identity_drift') else row.get('email') or (f'Desktop account {row["label"]}' if row.get('desktop_saved') else 'Identity not verified'),
                 self.color(base) | curses.A_BOLD, width-x)
        cli = row.get('cli_state', 'unknown')
        role = 14 if cli == 'ready' else 16 if cli == 'refreshing' else 17 if cli == 'sign_in_required' else 15
        display = {'not_selected': 'not selected', 'expired': 'refresh pending', 'refresh_failed': 'refresh failed', 'sign_in_required': 'sign-in required'}.get(cli, cli)
        x = self.badge(y+1, 5, 'CLI ' + display, role, width+2)
        if 'desktop' not in self.components:
            text, role = 'Desktop not selected', 13
        elif row.get('identity_drift'):
            text, role = 'LOGIN MISMATCH', 15
        elif row.get('desktop_active'):
            text, role = 'Desktop active', 16
        elif row.get('pending'):
            text, role = 'Desktop: finish sign-in', 15
        elif row.get('desktop_saved'):
            text, role = 'Desktop saved', 14
        else:
            text, role = 'Desktop: setup needed', 15
        self.badge(y+1, x, text, role, width+2)
        if row.get('identity_drift'):
            self.fill(y+2, 2, width, base)
            self.put(y+2, 5, 'Desktop: ' + str(row.get('desktop_email') or row.get('desktop_account')), self.color(15), width-4)
            y += 1
        usage = row.get('usage', {})
        windows = [w for w in usage.get('windows', []) if w['key'] in ('session', 'weekly_all')]
        for offset in (2, 3):
            self.fill(y+offset, 2, width, base)
            self.put(y+offset, 2, '▌' if selected else '│', self.color(10 if selected else 18))
        if windows:
            for offset, window in enumerate(windows[:2], 2):
                stale = usage.get('state') == 'stale'
                text = account_usage.summary(window)
                if stale:
                    text = 'STALE · ' + text
                role = 22 if stale or window['percent'] >= 80 else 9 if selected else 13
                self.put(y+offset, 5, text, self.color(role), width-4)
        else:
            detail = ('Usage needs a CLI login.' if 'cli' not in self.components else usage.get('detail') or 'Usage loads after CLI login is verified.')
            self.put(y+2, 5, 'Usage unavailable', self.color(9 if selected else 13), width-4)
            self.put(y+3, 5, detail, self.color(9 if selected else 13), width-4)
        if not curses.has_colors() and selected:
            self.put(y, 2, '>', curses.A_BOLD)

    def rotation_panel(self, width, height):
        status = self.rotation_status()
        self.prune_rotation_selection()
        self.put(3, 2, 'TOOLS / AUTO ROTATION', self.color(1) | curses.A_BOLD, width)
        for line in range(5, 9 if height >= 30 else 8):
            self.fill(line, 2, width, 7)
        health, role = self.rotation_health(status)
        x = self.badge(5, 4, health, role, width+2)
        self.put(5, x, 'Checks every 60s', self.color(13), width-x)
        active = status.get('active')
        account = next((a for a in self.state.get('accounts', []) if a['label'] == active), {})
        name = account.get('cli_email') or account.get('email') or active
        active_text = ('ACTIVE  ' if status['enabled'] and health == 'RUNNING' else 'LAST USED  ') + str(name) if active else 'No active account'
        self.put(6, 4, active_text, self.color(25 if status['enabled'] and health == 'RUNNING' else 13), width-4)
        saved = [a['label'] for a in status.get('accounts', [])]
        dirty = self.rotation_selected != saved
        selection = str(len(self.rotation_selected)) + ' accounts selected'
        selection += '  •  Select at least 2 to save' if len(self.rotation_selected) < 2 else '  •  Unsaved selection' if dirty else '  •  Selection saved' if status['enabled'] else '  •  Select at least 2 to enable'
        self.put(7, 4, selection, self.color(22 if dirty else 13), width-4)
        if height >= 30:
            stamp = status.get('checked_at')
            checked = 'Last check: '+time.strftime('%H:%M:%S', time.localtime(stamp)) if stamp else 'No checks yet'
            if health in ('ERROR', 'MONITOR DELAYED', 'CHECK UNAVAILABLE', 'WAITING FOR ACCOUNT'):
                checked += '  •  '+status.get('detail', 'Use Check quotas now to retry.')
            else:
                checked += '  •  Shared sessions: ~/.claude-rotate'
            self.put(8, 4, checked, self.color(22 if role in (15, 17) else 13), width-4)
        y = 10 if height >= 30 else 9
        if not self.rotation_accounts() and height >= 30:
            self.put(y, 2, 'No valid CLI logins. Sign in from Accounts, then refresh here.', self.color(3), width)
            y += 2
        return y

    def rotation_card(self, y, row, focused, width, compact=False):
        status = self.rotation_status()
        label = row['label']
        chosen = label in self.rotation_selected
        base = 8 if focused else 7
        lines = 2 if compact else 4
        for offset in range(lines):
            self.fill(y+offset, 2, width, base)
            self.put(y+offset, 2, '▌' if focused else '│', self.color(10 if focused else 18))
        self.put(y, 5, '[x]' if chosen else '[ ]', self.color(10 if focused else 25 if chosen else 13) | curses.A_BOLD)
        self.put(y, 10, label+'  '+str(row.get('cli_email') or row.get('email')), self.color(base) | curses.A_BOLD, width-9)
        monitored = next((p for p in status.get('profiles', []) if p['label'] == label), {})
        state = monitored.get('state', 'ready') if status['enabled'] else 'ready'
        text, role = {'ready': ('CLI VALID', 16), 'limited': ('LIMIT REACHED', 15),
                      'auth': ('SIGN-IN NEEDED', 17), 'unknown': ('CHECK UNAVAILABLE', 15),
                      'changed': ('ACCOUNT CHANGED', 17), 'removed': ('REMOVED', 15)}.get(state, ('CHECKING', 16))
        if status['enabled'] and status.get('active') == label:
            if state == 'ready' and self.rotation_health(status)[0] == 'RUNNING':
                text, role = 'ACTIVE NOW', 14
            else:
                text, role = 'LAST USED • CHECK STATUS', 15
        x = self.badge(y+1, 5, text, role, width+2)
        self.put(y+1, x, 'Selected for rotation' if chosen else 'Not selected', self.color(9 if focused else 13), width-x)
        if compact:
            return
        usage = monitored.get('usage') or row.get('usage') or {}
        if status['enabled'] and status.get('stale'):
            usage = {**usage, 'state': 'stale'}
        windows = [w for w in usage.get('windows', []) if w['key'] in ('session', 'weekly_all')]
        for offset, window in enumerate(windows[:2], 2):
            percent = window['percent']
            filled = min(10, max(0, round(percent/10)))
            short = '5h' if window['key'] == 'session' else '7d'
            bar = short+' ['+'█'*filled+'░'*(10-filled)+f'] {percent:g}% used'
            color = (29 if focused else 26) if percent >= 100 else (28 if focused else 22) if percent >= 80 or usage.get('state') != 'ready' else (27 if focused else 25)
            self.put(y+offset, 5, bar, self.color(color), width-4)
            stamp = window.get('resets_at')
            reset = 'resets '+time.strftime('%a %H:%M', time.localtime(stamp))+' ('+account_usage.countdown(stamp)+')' if stamp else 'Reset time unavailable'
            if usage.get('state') != 'ready':
                reset = 'STALE • '+reset
            self.put(y+offset, 5+len(bar)+2, reset, self.color(9 if focused else 13), width-len(bar)-7)
        if not windows:
            self.put(y+2, 5, 'Usage not available yet', self.color(9 if focused else 13), width-4)
            self.put(y+3, 5, 'Use Check quotas now' if status['enabled'] else 'Usage will update when rotation is enabled', self.color(9 if focused else 13), width-4)

    def draw(self):
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if height < 20 or width < 60:
            self.put(1, 2, 'CLAUDE ACCOUNTS', curses.A_BOLD | self.color(1))
            self.put(3, 2, 'Enlarge terminal to 60 columns x 20 rows.', self.color(4))
            self.put(5, 2, 'Esc: back   q: quit when idle', self.color(1))
            self.screen.refresh()
            return
        cw = min(width-4, 96)
        self.fill(1, 2, cw, 7)
        self.put(1, 4, 'CLAUDE ACCOUNTS', curses.A_BOLD | self.color(18))
        if self.demo:
            self.put(1, 23, 'DEMO', self.color(19))
        if self.page == 'rotation':
            y = self.rotation_panel(cw, height)
        else:
            active = next((a for a in self.state.get('accounts', []) if a.get('desktop_active')), None)
            desktop = self.state.get('active_email') or ((active.get('desktop_email') or active.get('desktop_account') or active.get('email') or active['label']) if active else self.state.get('desktop_detail', 'Checking...'))
            running = self.state.get('desktop_running')
            self.put(3, 2, 'DESKTOP', self.color(4))
            self.put(3, 11, str(desktop), self.color(2 if active else 4), cw-10)
            if running is not None and len(str(desktop)) + 26 < cw:
                status = '● running' if running else '◐ background tasks' if self.state.get('desktop_background') else '○ closed'
                self.put(3, 13+len(str(desktop)), status, self.color(2 if running else 3 if self.state.get('desktop_background') else 4))
            if self.page == 'home':
                y = 5
                if self.state.get('first_run'):
                    self.put(y, 2, 'WELCOME — connect your first Claude account', self.color(1) | curses.A_BOLD, cw)
                    self.put(y+1, 2, 'Sign into the Claude apps you selected during installation.', self.color(4), cw)
                    y += 3
            else:
                title, intro = self.header()
                self.put(5, 2, title, curses.A_BOLD | self.color(1), cw)
                y = 7
                if self.page == 'wizard':
                    stage = self.wizard['stage']
                    current = {'name': 0, 'cli': 0, 'desktop': 1, 'finish': 2, 'complete': 3}[stage]
                    x = 2
                    steps = ('1 CLI', '2 Desktop', '3 Save')
                    if 'cli' not in self.components:
                        steps = ('1 Desktop', '2 Save')
                        current = {'name': 0, 'desktop': 0, 'finish': 1, 'complete': 2}.get(stage, 0)
                    elif 'desktop' not in self.components:
                        steps = ('1 CLI',)
                        current = 1 if stage == 'complete' else 0
                    for i, step in enumerate(steps):
                        x = self.badge(y, x, step, 14 if i < current else 16 if i == current else 13, cw+2)
                    y += 2
                # Keep enough room for an actionable menu even in a short terminal.
                lines = [part for line in intro for part in (textwrap.wrap(clean(line), cw) or [''])]
                budget = max(1, height-y-8)
                for line in lines[:budget]:
                    self.put(y, 2, line, self.color(2 if line == 'Already selected in Desktop.' else 4), cw)
                    y += 1
                if len(lines) > budget:
                    self.put(y-1, cw-1, '…', self.color(4))
                y += 1
        name_input = self.page == 'wizard' and self.wizard['stage'] == 'name'
        if name_input:
            self.fill(y, 2, cw, 8)
            self.put(y, 4, 'Label: ' + self.input_text + '_', self.color(8) | curses.A_BOLD, cw-4)
            y += 2
            footer = 'Type a label   Enter: create profile   Esc: cancel'
            detail = ''
        else:
            rows = self.items()
            self.cursor = min(self.cursor, max(0, len(rows)-1))
            # Scroll a single list, with non-focusable section dividers. Account
            # Cards contain identity, status and usage plus breathing room.
            def layout(start):
                result, position, previous = [], y, None
                for i in range(start, len(rows)):
                    action = rows[i][0]
                    group = ('ACCOUNTS • SPACE TO SELECT' if self.page == 'rotation' else 'ACCOUNTS') if action.startswith(('account:', 'rotate_pick:')) else 'ACTIONS'
                    heading = self.page in ('home', 'rotation') and group != previous
                    size = (6 if next(a for a in self.state['accounts'] if a['label'] == action.split(':', 1)[1]).get('identity_drift') else 5) if action.startswith('account:') else 1
                    if action.startswith('rotate_pick:'):
                        size = 3 if height < 30 else 5
                    if position + size + int(heading) > height-5:
                        break
                    if heading:
                        result.append(('heading', position, group))
                        position += 1
                    result.append(('item', position, i))
                    position += size
                    previous = group
                return result, position
            start = 0
            blocks, end = layout(start)
            while rows and not any(kind == 'item' and value == self.cursor for kind, _, value in blocks):
                start += 1
                blocks, end = layout(start)
                if start >= len(rows):
                    break
            for kind, line, value in blocks:
                if kind == 'heading':
                    self.rule(line, value, cw)
                    continue
                action, name, _ = rows[value]
                selected = value == self.cursor
                if action.startswith('rotate_pick:'):
                    row = next(a for a in self.rotation_accounts() if a['label'] == action.split(':', 1)[1])
                    self.rotation_card(line, row, selected, cw, compact=height < 30)
                elif action.startswith('account:'):
                    row = next(a for a in self.state['accounts'] if a['label'] == action.split(':', 1)[1])
                    self.account_card(line, row, selected, cw)
                else:
                    self.fill(line, 2, cw, 8 if selected else 7)
                    icon = '›' if selected else '+' if action in ('add', 'rotate_enable') else '‹' if action in ('back', 'pause', 'done') else '·'
                    if self.page == 'rotation' and not selected:
                        icon = {'rotate_enable': '+', 'rotate_disable': '■', 'rotate_check': '↻', 'refresh': '↻', 'rotate_help': '?', 'back': '‹'}.get(action, icon)
                    self.put(line, 3, icon, self.color(10 if selected else 26 if action == 'rotate_disable' else 19))
                    self.put(line, 6, name.removeprefix('+ '),
                             self.color(8 if selected else 25 if action == 'rotate_enable' else 26 if action == 'rotate_disable' else 19 if action in ('add', 'rotate_help') else 7) | (curses.A_BOLD if selected or action == 'add' else 0), cw-5)
                    if not curses.has_colors() and selected:
                        self.put(line, 3, '>', curses.A_BOLD)
            y = end
            detail = rows[self.cursor][2] if rows else ''
            # Account statuses are already shown as badges; give the selection a purpose.
            if rows and rows[self.cursor][0].startswith('account:'):
                selected_account = next(a for a in self.state['accounts'] if a['label'] == rows[self.cursor][0].split(':', 1)[1])
                detail = ('Manage the login for the current Desktop account.' if selected_account.get('desktop_active')
                          else 'Open this account to switch Desktop or manage its login.')
            footer = ('↑/↓ move   Space: toggle   Enter: select   Esc: back' if self.page == 'rotation' else '↑/↓ move   Enter: select   Esc: back')
            if sum(kind == 'item' for kind, _, _ in blocks) < len(rows):
                footer += f'   {self.cursor+1}/{len(rows)}'
        y = min(y+1, height-4)
        if self.worker:
            spinner = '|/-\\'[int(time.monotonic()*8) % 4]
            status = 'Checking logins...' if self.job == 'status' else 'Working... Please wait.'
            self.put(y, 2, spinner + ' ' + status, self.color(1), cw)
        elif self.cli_worker:
            self.put(y, 2, f'Refreshing CLI login for account {self.cli_worker.label} in background…', self.color(1), cw)
        elif self.notice:
            self.put(y, 2, self.notice, self.color(3), cw)
        else:
            self.put(y, 2, detail, self.color(4), cw)
        self.put(y+1, 2, '─' * cw, self.color(6), cw)
        self.put(y+2, 2, footer, self.color(1), cw)
        if self.modal:
            self._modal_paint = True
            try:
                self.draw_modal()
            finally:
                self._modal_paint = False
        self.screen.refresh()

    def draw_modal(self):
        kind, action, title, lines = self.modal
        height, width = self.screen.getmaxyx()
        panel_width = min(width-8, 68)
        inner = panel_width-8
        content = []
        if kind == 'confirm' and action in ('switch', 'save', 'cli') and self.account:
            account = self.account
            content += [(part, 18) for part in textwrap.wrap(
                f'{account.get("email") or "Unverified account"}  [{account["label"]}]', inner)]
            content += [('', 7)]
        for i, line in enumerate(lines):
            if i:
                content.append(('', 7))
            warning = kind == 'confirm' and ('will quit' in line or 'will pause' in line)
            text = ('! ' if warning else '') + clean(line)
            content += [(part, 22 if warning else 7) for part in (textwrap.wrap(text, inner) or [''])]
        panel_height = min(height-4, max(12, len(content)+9))
        x, y = (width-panel_width)//2, (height-panel_height)//2
        # A subtle shadow and complete border isolate the dialog from the menu.
        for row in range(y+1, y+panel_height+1):
            self.fill(row, x+2, panel_width, 23)
        for row in range(y, y+panel_height):
            self.fill(row, x, panel_width, 7)
            self.put(row, x, '│', self.color(21))
            self.put(row, x+panel_width-1, '│', self.color(21))
        self.put(y, x, '╭'+'─'*(panel_width-2)+'╮', self.color(21), panel_width)
        self.put(y+panel_height-1, x, '╰'+'─'*(panel_width-2)+'╯', self.color(21), panel_width)
        self.put(y+1, x+4, 'CONFIRM ACTION' if kind == 'confirm' else 'RESULT' if kind == 'result' else 'DETAILS', self.color(13), inner)
        self.put(y+2, x+4, title, curses.A_BOLD | self.color(18), inner)
        capacity = panel_height-9
        self.scroll = max(0, min(self.scroll, max(0, len(content)-capacity)))
        for i, (line, role) in enumerate(content[self.scroll:self.scroll+capacity]):
            self.put(y+4+i, x+4, line, self.color(role), inner)
        divider = y+panel_height-4
        self.put(divider, x+3, '─'*(panel_width-6), self.color(21), panel_width-6)
        label = {'switch': 'Switch', 'save': 'Save login', 'cli': 'Open CLI',
                 'recover': 'Recover', 'sync': 'Sync history'}.get(action, 'Continue')
        if action and action.startswith('manage:remove'):
            label = 'Remove profile'
        elif action and action.startswith('manage:restore'):
            label = 'Restore profile'
        elif action and action.startswith('manage:reconcile:'):
            label = 'Apply repair'
        elif action and action.startswith('close_save:'):
            label = 'Close Desktop and save'
        elif action and action.startswith('stop_save:'):
            label = 'Stop tasks and save'
        elif action and action.startswith('retry_save:'):
            label = 'Retry save'
        primary = ' Enter: ' + (label if kind == 'confirm' else 'Close') + ' '
        secondary = ' Esc: ' + ('Cancel' if kind == 'confirm' else 'Close') + ' '
        self.put(divider+1, x+4, primary, self.color(24) | curses.A_BOLD, len(primary))
        self.put(divider+1, x+6+len(primary), secondary, self.color(13), len(secondary))
        if len(content) > capacity:
            self.put(y+panel_height+1, x+3, '↑/↓ scroll for more', self.color(4), panel_width-6)

    def key(self, key):
        if self.worker:
            # Status is read-only. Keep navigation responsive and retain Enter
            # instead of silently swallowing a new profile's first login action.
            if self.job == 'status' and not self.modal and not (self.page == 'wizard' and self.wizard['stage'] == 'name'):
                rows = self.items()
                if key in (curses.KEY_UP, ord('k'), curses.KEY_DOWN, ord('j')):
                    delta = -1 if key in (curses.KEY_UP, ord('k')) else 1
                    self.cursor = (self.cursor + delta) % max(1, len(rows))
                    self.deferred_action = None
                elif key in (10, 13, curses.KEY_ENTER) and rows:
                    self.deferred_action = (self.page, self.label, (self.wizard or {}).get('stage'), rows[self.cursor][0])
                    self.notice = 'Checking logins; your selection will open when ready.'
            return  # A profile transaction must finish before the UI can close.
        if self.modal:
            kind, action, _, _ = self.modal
            if key == curses.KEY_UP:
                self.scroll -= 1
            elif key == curses.KEY_DOWN:
                self.scroll += 1
            elif key in (10, 13, curses.KEY_ENTER, 27):
                self.modal = None
                if key != 27 and kind == 'confirm':
                    self.start(action)
                elif kind == 'result':
                    self.refresh()
            return
        if self.page == 'wizard' and self.wizard['stage'] == 'name':
            if key == 27:
                self.wizard = None
                self.goto('home')
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                self.input_text = self.input_text[:-1]
            elif key in (10, 13, curses.KEY_ENTER):
                from onboarding import validate
                try:
                    validate(self.input_text)
                    if self.input_text in [a['label'] for a in self.state.get('accounts', [])]:
                        raise RuntimeError('That label already exists. Choose it from the accounts menu to resume setup.')
                except RuntimeError as error:
                    self.notice = str(error)
                    return
                self.label = self.input_text
                self.start_onboarding('create')
            elif 32 <= key < 127 and len(self.input_text) < 32:
                self.input_text += chr(key)
            return
        rows = self.items()
        if key in (curses.KEY_UP, ord('k'), curses.KEY_DOWN, ord('j')):
            delta = -1 if key in (curses.KEY_UP, ord('k')) else 1
            self.cursor = (self.cursor+delta) % max(1, len(rows))
        elif key == ord(' ') and self.page == 'rotation' and rows and rows[self.cursor][0].startswith('rotate_pick:'):
            self.dispatch(rows[self.cursor][0])
        elif key in (10, 13, curses.KEY_ENTER) and rows:
            self.dispatch(rows[self.cursor][0])
        elif key == 27:
            if self.page != 'home':
                self.dispatch('back')
        elif key == ord('q'):
            self.running = False
        elif key == ord('r'):
            self.refresh()
        elif key == ord('?'):
            self.message('Using Claude Accounts', ['Use Up/Down to choose a row and Enter to select it. Esc goes back.',
                'Add new account guides you through CLI sign-in, Desktop sign-in, and verification.',
                'Choose Set up Desktop login for an account without a saved Desktop login.',
                'If sign-in is already in progress, choose Finish Desktop sign-in to resume.',
                'If Desktop is already using an unsaved account, choose Save current Desktop login.',
                'Switch Desktop restores a saved login. You do not need to log out between switches.',
                'Expired or revoked Desktop logins may still require browser sign-in.'])

    def run(self):
        interrupted = False
        def interrupt(*_):
            nonlocal interrupted
            if not self.worker:
                interrupted = True
                self.running = False
        previous = signal.signal(signal.SIGINT, interrupt)
        try:
            while self.running:
                self.poll()
                self.draw()
                key = self.screen.getch()
                if key != -1:
                    self.key(key)
        except KeyboardInterrupt:
            interrupted = True
        finally:
            # Keep the non-raising handler installed throughout cleanup. Repeated
            # Ctrl+C must not interrupt worker shutdown or curses restoration.
            if self.cli_worker:
                self.cli_worker.close()
            if interrupted:
                # Erase the alternate screen before endwin can save it in history.
                try:
                    self.screen.erase()
                    self.screen.refresh()
                except curses.error:
                    pass
            signal.signal(signal.SIGINT, previous)
        return 130 if interrupted else 0


def clear_terminal_after_interrupt():
    if sys.stdout.isatty():
        try:
            # Run after curses restores the shell screen: clear the visible
            # screen, then scrollback, and put the shell prompt at the top.
            sys.stdout.write('\x1b[2J\x1b[3J\x1b[H')
            sys.stdout.flush()
        except OSError:
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo', action='store_true', help='preview with fictional accounts; actions disabled')
    parser.add_argument('--onboard', action='store_true', help='open setup automatically for a new user')
    parser.add_argument('--cli', metavar='LABEL', help='open a CLI profile without shell aliases; put CLI options after --')
    parser.add_argument('cli_args', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.cli:
        if args.demo:
            parser.error('--cli cannot be combined with --demo')
        try:
            if args.cli in dp.Profiles(Path.home(), None, None).index().get('removedProfiles', {}):
                parser.error('This profile was removed. Restore it from Tools → Removed profiles.')
            env = cli_environment(args.cli)
        except ValueError as error:
            parser.error(str(error))
        if args.cli != 'a' and not (Path.home()/('.claude-acct-'+args.cli)).is_dir():
            parser.error('No such CLI profile. Add the account in the TUI first.')
        executable = shutil.which('claude')
        if not executable:
            parser.error('Claude Code is not installed or not on PATH.')
        extra = args.cli_args[1:] if args.cli_args[:1] == ['--'] else args.cli_args
        command = [executable, *extra]
        if sys.stdin.isatty() and sys.stdout.isatty():
            command = runtime.interactive_command(command)
        return subprocess.call(command, env=env)
    if args.cli_args:
        parser.error('CLI options require --cli LABEL')
    if sys.platform != 'darwin' and not args.demo:
        parser.error('Desktop account management currently supports macOS only.')
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print('Open claude-accounts in an interactive terminal. Use claude-desktop-sync --status-json for scripts.', file=sys.stderr)
        return 2
    try:
        result = curses.wrapper(lambda screen: App(screen, args.demo, args.onboard).run()) or 0
    except KeyboardInterrupt:
        # Covers startup and the small window after restoring the old handler.
        result = 130
    except curses.error:
        print('The terminal could not be initialized. Use a terminal with TERM=xterm-256color.', file=sys.stderr)
        return 1
    if result == 130:
        clear_terminal_after_interrupt()
    return result


if __name__ == '__main__':
    sys.exit(main())
