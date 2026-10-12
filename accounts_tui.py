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
import textwrap
import threading
import time
from cli_refresh import RefreshWorker
import account_usage
import runtime
import setup_options

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
    env = dict(os.environ if environ is None else environ)
    for key in ('CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN'):
        env.pop(key, None)
    if label == 'a':
        env.pop('CLAUDE_CONFIG_DIR', None)
    else:
        env['CLAUDE_CONFIG_DIR'] = str((home or Path.home()) / ('.claude-acct-' + label))
    return env


def disabled_reason(action, account, state):
    if state.get('recovery_pending') and action in ('switch', 'save', 'login', 'sync'):
        return 'An interrupted switch needs recovery first. Open Tools and select Recover interrupted switch.'
    if action == 'recover' and not state.get('recovery_pending'):
        return 'There is no interrupted switch to recover.'
    if action in ('switch', 'save', 'login', 'cli') and not account:
        return 'Wait for accounts to load, then select an account.'
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
        if self.demo or self.modal or self.page != 'wizard' or self.wizard['stage'] != 'finish':
            return
        now = time.monotonic()
        if now - self.desktop_checked < 1.5 or now - self.last_refresh < 3:
            return
        self.desktop_checked = now
        # Observe local file changes; avoid repeatedly querying account APIs
        # while the user is just looking at an unchanged sign-in screen.
        if self.desktop_signature() != self.desktop_seen:
            self.refresh()

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
                self.wizard['stage'] = 'desktop' if 'desktop' in self.components else 'complete'
                self.cursor = 0
                self.notice = 'CLI verified: ' + row['email']
            else:
                self.message('CLI sign-in needs attention', [row.get('cli_detail', '') if row else 'Account status is unavailable.',
                                                          'Retry sign-in or check the existing login to continue.'])
        elif self.page == 'wizard' and self.wizard['stage'] in ('desktop', 'finish'):
            row = self.account or {}
            if row.get('desktop_active'):
                self.wizard['stage'] = 'complete' if row.get('desktop_saved') and not row.get('pending') else 'finish'
                self.cursor = 0
                self.notice = ('Desktop login is already saved.' if self.wizard['stage'] == 'complete'
                               else 'Desktop sign-in detected. Choose Verify and finish setup to save it.')

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
                        row.update(email_source='verified with Anthropic', desktop_active=result['account'] == self.state.get('active_account'))
            if result['cli_state'] == 'ready':
                self.notice = f'CLI login refreshed for account {worker.label}.'
                self.refresh_pending = True
            else:
                self.notice = f'Account {worker.label}: ' + result['cli_detail']
            if self.pending_cli_check == worker.label:
                self.pending_cli_check = None
                if self.page == 'wizard' and self.label == worker.label:
                    if result['cli_state'] == 'ready':
                        self.wizard['stage'] = ('finish' if self.account.get('pending') else 'desktop') if 'desktop' in self.components else 'complete'
                        self.cursor = 0
                    else:
                        self.message('CLI sign-in needs attention', [result['cli_detail']])
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
                elif value:
                    self.message('Action needs attention', self.logs)
                    if job.startswith('onboard:'):
                        self.refresh()
                elif job.startswith('onboard:'):
                    action = job.split(':')[1]
                    self.wizard['stage'] = {'create': 'cli', 'prepare': 'finish', 'finish': 'complete', 'create-desktop': 'desktop', 'prepare-desktop': 'finish', 'finish-desktop': 'complete'}[action]
                    self.cursor = 0
                    self.notice = 'Profile created.' if action == 'create' else ''
                    if action in ('prepare', 'finish', 'prepare-desktop', 'finish-desktop'):
                        self.refresh()
                else:
                    self.modal = ('result', None, 'Finished', self.logs[:])
                    self.scroll = 0
                return

    def message(self, title, lines):
        self.modal, self.scroll = ('message', None, title, lines), 0

    def confirm(self, action, title, lines):
        self.modal, self.scroll = ('confirm', action, title, lines), 0

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
        if self.page == 'account':
            a = self.account or {}
            desktop = [('switch', 'Switch Desktop to this account', 'Save the current account automatically, then restore this login')] if a.get('desktop_saved') and not a.get('desktop_active') else []
            setup = []
            if not a.get('desktop_saved'):
                if a.get('pending'):
                    setup = [('setup', 'Finish Desktop sign-in', 'Continue the sign-in you started, then verify and save it')]
                elif not a.get('desktop_active'):
                    detail = ('Sign in once in Desktop and save the login for switching'
                              if a.get('cli_state') == 'ready' else 'First verify your CLI login, then sign in to Desktop' if 'cli' in self.components else 'Sign in to Desktop and save this account')
                    setup = [('setup', 'Set up Desktop login', detail)]
            retry = [('retry_refresh', 'Retry background refresh', 'Try renewing this CLI login without opening the interactive CLI')] if a.get('cli_state') in ('refresh_failed', 'sign_in_required') else []
            rows = desktop + setup + retry + [('usage', 'View usage limits', 'All usage windows, reset dates and last update time'),
                              ('cli', 'Open CLI', 'Open an interactive Claude session; /exit returns here'),
                              ('auth', 'Sign in to CLI', 'Open the official browser sign-in flow'),
                              ('save', 'Save current Desktop login', 'Verify it matches this CLI account first' if 'cli' in self.components else 'Verify and save the signed-in Desktop account'),
                              ('back', 'Back to accounts', '')]
            if 'cli' not in self.components:
                rows = [r for r in rows if r[0] not in ('cli', 'auth', 'retry_refresh', 'usage')]
            if 'desktop' not in self.components:
                rows = [r for r in rows if r[0] not in ('switch', 'setup', 'save')]
            return rows
        if self.page == 'tools':
            rows = [('sync', 'Sync local Code session history', 'Copy local session entries across accounts')] if set(setup_options.CHOICES).issubset(self.components) else []
            if self.state.get('recovery_pending'):
                rows.insert(0, ('recover', 'Recover interrupted switch', 'Restore the original Desktop profile'))
            return rows + [('back', 'Back to accounts', '')]
        if self.page == 'wizard':
            stage = self.wizard['stage']
            if stage == 'cli':
                rows = [('wizard_auth', 'Sign in to CLI', 'Browser sign-in opens; you return here when finished'),
                        ('verify_cli', 'Check existing CLI login', 'Continue if this profile is already signed in')]
                if self.account and self.account.get('cli_state') == 'expired':
                    rows.insert(0, ('cli', 'Refresh existing CLI login', 'Open Claude and use /exit to return'))
            elif stage == 'desktop':
                rows = [('prepare', 'Open Desktop sign-in', 'Preserve the current login and prepare this account')]
            elif stage == 'finish':
                rows = [('finish', 'Verify and finish setup', 'Verify and save the signed-in Desktop account' if 'cli' not in self.components else 'Check Desktop matches the CLI, then save its login'),
                        ('prepare', 'Reopen Desktop', 'Resume the existing sign-in screen')]
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
        if self.cli_worker and self.label == self.cli_worker.label and action in ('cli', 'auth', 'wizard_auth', 'setup', 'save', 'prepare', 'finish', 'verify_cli', 'retry_refresh'):
            self.notice = 'This CLI login is refreshing in the background. Please wait before using its login actions.'
            return
        if action.startswith('account:'):
            self.label = action.split(':', 1)[1]
            self.goto('account')
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
        elif action in ('cli', 'auth', 'wizard_auth'):
            if action == 'cli':
                self.confirm(action, 'Open Claude CLI', ['Use /exit to return here after Claude refreshes its login.',
                                                      'This launch uses safe mode (custom hooks and plugins disabled).'])
            else:
                self.launch_cli(self.label, authenticate=True)
        elif action in ('prepare', 'finish', 'prepare-desktop', 'finish-desktop'):
            self.start_onboarding(action)
        elif action == 'save' and 'cli' not in self.components:
            self.confirm('save_desktop', 'Save Desktop login?', [f'Verify and save the signed-in Desktop account as {self.label}.',
                         'Desktop will quit and reopen. Running Desktop sessions will pause.'])
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
        if action == 'save_desktop':
            self.begin_setup()
            self.start_onboarding('finish')
            return
        if action == 'cli':
            self.launch_cli(self.label)
            return
        if self.demo:
            self.message('Demo mode', ['Actions are disabled. No profiles were changed.'])
            return
        self.logs = []
        self.worker = Worker(helper_args(action, self.label or 'a'))
        self.job = action

    def launch_cli(self, label, authenticate=False):
        if self.demo:
            self.message('Demo mode', ['CLI launch is disabled in demo mode.'])
            return
        executable = shutil.which('claude')
        if not executable:
            self.message('CLI unavailable', ['Could not find claude on PATH.'])
            return
        command = [executable, 'auth', 'login', '--claudeai'] if authenticate else [executable, '--safe-mode']
        if authenticate and self.account and '@' in (self.account.get('email') or ''):
            command += ['--email', self.account['email']]
        result = 1
        curses.def_prog_mode()
        curses.endwin()
        try:
            print(f'Claude account {label}: ' + ('complete sign-in in your browser.' if authenticate else 'use /exit to return.'), flush=True)
            result = subprocess.run(command, env=cli_environment(label), check=False).returncode
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
        if result:
            self.message('CLI sign-in was not completed' if authenticate else 'CLI stopped',
                         ['You can retry. Your profile and setup progress are kept.'] + self.logs[-2:])
        elif self.page == 'wizard':
            self.refresh('verify_cli')
        else:
            self.refresh()

    def header(self):
        if self.page == 'home':
            return 'Choose an account', ['One selection at a time. Press Enter to open it.']
        if self.page == 'tools':
            return 'Accounts / Tools', ['Maintenance actions']
        a = self.account or {}
        who = a.get('email') or self.label or 'new account'
        if self.page == 'account':
            details = [a.get('cli_detail', 'Account status is loading.')]
            if a.get('desktop_active'):
                details.insert(0, 'Already selected in Desktop.')
            return 'Accounts / ' + str(who), details
        stage = self.wizard['stage']
        titles = {'name': 'Add account — choose a label', 'cli': 'Step 1 of 3 — CLI sign-in',
                  'desktop': 'Step 2 of 3 — Desktop sign-in', 'finish': 'Step 3 of 3 — Verify and save',
                  'complete': 'Account setup complete'}
        descriptions = {
            'name': ['Choose a short label, for example d or work. It creates claude-<label>.',
                     'Existing CLI settings and local history are shared; the login is separate.'],
            'cli': [f'Profile: {self.label}. Sign in with the account you want to add.',
                    'The official CLI stores the login in your macOS Keychain.'],
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
        self.put(y, x, row.get('email') or (f'Desktop account {row["label"]}' if row.get('desktop_saved') else 'Identity not verified'),
                 self.color(base) | curses.A_BOLD, width-x)
        cli = row.get('cli_state', 'unknown')
        role = 14 if cli == 'ready' else 16 if cli == 'refreshing' else 17 if cli == 'sign_in_required' else 15
        display = {'not_selected': 'not selected', 'expired': 'refresh pending', 'refresh_failed': 'refresh failed', 'sign_in_required': 'sign-in required'}.get(cli, cli)
        x = self.badge(y+1, 5, 'CLI ' + display, role, width+2)
        if 'desktop' not in self.components:
            text, role = 'Desktop not selected', 13
        elif row.get('desktop_active'):
            text, role = 'Desktop active', 16
        elif row.get('pending'):
            text, role = 'Desktop: finish sign-in', 15
        elif row.get('desktop_saved'):
            text, role = 'Desktop saved', 14
        else:
            text, role = 'Desktop: setup needed', 15
        self.badge(y+1, x, text, role, width+2)
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
        active = next((a for a in self.state.get('accounts', []) if a.get('desktop_active')), None)
        desktop = (active.get('email') or active['label']) if active else self.state.get('desktop_detail', 'Checking...')
        running = self.state.get('desktop_running')
        self.put(3, 2, 'DESKTOP', self.color(4))
        self.put(3, 11, str(desktop), self.color(2 if active else 4), cw-10)
        if running is not None and len(str(desktop)) + 26 < cw:
            self.put(3, 13+len(str(desktop)), '● running' if running else '○ closed', self.color(2 if running else 4))
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
                    group = 'ACCOUNTS' if action.startswith('account:') else 'ACTIONS'
                    heading = self.page == 'home' and group != previous
                    size = 5 if action.startswith('account:') else 1
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
                if action.startswith('account:'):
                    row = next(a for a in self.state['accounts'] if a['label'] == action.split(':', 1)[1])
                    self.account_card(line, row, selected, cw)
                else:
                    self.fill(line, 2, cw, 8 if selected else 7)
                    icon = '›' if selected else '+' if action == 'add' else '‹' if action in ('back', 'pause', 'done') else '·'
                    self.put(line, 3, icon, self.color(10 if selected else 19))
                    self.put(line, 6, name.removeprefix('+ '),
                             self.color(8 if selected else 19 if action == 'add' else 7) | (curses.A_BOLD if selected or action == 'add' else 0), cw-5)
                    if not curses.has_colors() and selected:
                        self.put(line, 3, '>', curses.A_BOLD)
            y = end
            detail = rows[self.cursor][2] if rows else ''
            # Account statuses are already shown as badges; give the selection a purpose.
            if rows and rows[self.cursor][0].startswith('account:'):
                selected_account = next(a for a in self.state['accounts'] if a['label'] == rows[self.cursor][0].split(':', 1)[1])
                detail = ('Manage the login for the current Desktop account.' if selected_account.get('desktop_active')
                          else 'Open this account to switch Desktop or manage its login.')
            footer = '↑/↓ move   Enter: select   Esc: back'
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
        primary = ' Enter: ' + (label if kind == 'confirm' else 'Close') + ' '
        secondary = ' Esc: ' + ('Cancel' if kind == 'confirm' else 'Close') + ' '
        self.put(divider+1, x+4, primary, self.color(24) | curses.A_BOLD, len(primary))
        self.put(divider+1, x+6+len(primary), secondary, self.color(13), len(secondary))
        if len(content) > capacity:
            self.put(y+panel_height+1, x+3, '↑/↓ scroll for more', self.color(4), panel_width-6)

    def key(self, key):
        if self.worker:
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
        def interrupt(*_):
            if not self.worker:
                raise KeyboardInterrupt
        previous = signal.signal(signal.SIGINT, interrupt)
        try:
            while self.running:
                self.poll()
                self.draw()
                key = self.screen.getch()
                if key != -1:
                    self.key(key)
        except KeyboardInterrupt:
            pass
        finally:
            signal.signal(signal.SIGINT, previous)
            if self.cli_worker:
                self.cli_worker.close()


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
            env = cli_environment(args.cli)
        except ValueError as error:
            parser.error(str(error))
        if args.cli != 'a' and not (Path.home()/('.claude-acct-'+args.cli)).is_dir():
            parser.error('No such CLI profile. Add the account in the TUI first.')
        executable = shutil.which('claude')
        if not executable:
            parser.error('Claude Code is not installed or not on PATH.')
        extra = args.cli_args[1:] if args.cli_args[:1] == ['--'] else args.cli_args
        return subprocess.call([executable, *extra], env=env)
    if args.cli_args:
        parser.error('CLI options require --cli LABEL')
    if sys.platform != 'darwin' and not args.demo:
        parser.error('Desktop account management currently supports macOS only.')
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print('Open claude-accounts in an interactive terminal. Use claude-desktop-sync --status-json for scripts.', file=sys.stderr)
        return 2
    try:
        curses.wrapper(lambda screen: App(screen, args.demo, args.onboard).run())
    except curses.error:
        print('The terminal could not be initialized. Use a terminal with TERM=xterm-256color.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
