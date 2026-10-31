"""Codex account management using official CLI helpers and shared local history."""
import argparse
import curses
import json
from pathlib import Path
import queue
import re
import subprocess
import sys
import textwrap
import time

import account_usage
from accounts_tui import App as BaseApp, Worker
import codex_install
from desktop_profiles import atomic_json
import runtime


HELPER = Path(__file__).with_name('codex_accounts.py')
ROTATION = Path(__file__).with_name('codex_rotation.py')
COMPONENTS = ('cli', 'desktop')
BACK_TO_PROVIDERS = 20


def preference_path(home=None):
    return (home or Path.home())/'.config/claude-accounts/codex-preferences.json'


def load_components(home=None):
    try:
        values = json.loads(preference_path(home).read_text())['components']
        if isinstance(values, list) and values and all(v in COMPONENTS for v in values):
            return [v for v in COMPONENTS if v in values]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return list(COMPONENTS)


def save_components(values, home=None):
    selected = [v for v in COMPONENTS if v in values]
    if not selected:
        raise ValueError('Select CLI, Desktop, or both.')
    path = preference_path(home)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_json(path, {'components': selected})


def duration_label(window):
    minutes = window.get('window_minutes', window.get('windowMinutes', window.get('duration_minutes')))
    if isinstance(minutes, (int, float)) and minutes > 0:
        if minutes % 1440 == 0:
            return f'{minutes/1440:g}d'
        if minutes % 60 == 0:
            return f'{minutes/60:g}h'
        return f'{minutes:g}m'
    return str(window.get('label') or window.get('key') or 'Usage')


class App(BaseApp):
    def __init__(self, screen, demo=False, onboard=False):
        self.rotation_worker = None
        self.rotation_data = {'enabled': False, 'accounts': []}
        self.rotation_selected = []
        self.rotation_loaded = False
        self.rotation_errors = []
        self.back_to_providers = False
        self.detected = ({'cli': True, 'desktop': True} if demo else
                         {'cli': bool(codex_install.detect_cli()), 'desktop': bool(codex_install.detect_desktop())})
        super().__init__(screen, demo, onboard)
        self.components = list(COMPONENTS) if demo else load_components()

    def refresh(self, after=None):
        if self.worker:
            return
        self.after_status = after
        self.last_refresh = time.monotonic()
        if self.demo:
            now = time.time()
            windows = [{'key': 'primary', 'label': 'Session', 'window_minutes': 300, 'percent': 24, 'resets_at': now+7200},
                       {'key': 'secondary', 'label': 'Weekly', 'window_minutes': 10080, 'percent': 48, 'resets_at': now+172800}]
            self.state = {'accounts': [
                {'label': 'personal', 'email': 'personal@example.test', 'cli_state': 'ready', 'desktop_saved': True,
                 'desktop_active': True, 'cli_detail': 'One verified login for CLI and Desktop.', 'usage': {'state': 'ready', 'windows': windows}},
                {'label': 'work', 'email': 'work@example.test', 'cli_state': 'ready', 'desktop_saved': True,
                 'desktop_active': False, 'cli_detail': 'One verified login for CLI and Desktop.', 'usage': {'state': 'ready', 'windows': [dict(w, percent=10) for w in windows]}},
                {'label': 'new', 'email': None, 'cli_state': 'missing', 'desktop_saved': False, 'desktop_active': False,
                 'cli_detail': 'Sign in once to use CLI and Desktop.'}],
                'desktop_running': True, 'desktop_managed_running': True, 'desktop_background': False, 'active_email': 'personal@example.test',
                'removed_profiles': [], 'recovery_pending': False}
            return
        self.worker = Worker(runtime.command(HELPER, '--status-json'), status=True)
        self.job = 'status'
        self.logs = []
        self.status_received = False
        if self.rotation_worker is None:
            self.rotation_errors = []
            self.rotation_worker = Worker(runtime.command(ROTATION, '--status'), status=True)

    def rotation_status(self):
        return self.rotation_data

    def desktop_uses_account(self, row):
        return bool(row.get('desktop_active') and self.state.get('desktop_managed_running', self.state.get('desktop_running')))

    def rotation_health(self, status):
        if status.get('state') in ('busy', 'deferred', 'waiting_idle') and status.get('enabled'):
            return 'WAITING FOR IDLE', 15
        return super().rotation_health(status)

    def poll(self):
        if self.rotation_worker:
            while True:
                try:
                    event, value = self.rotation_worker.events.get_nowait()
                except queue.Empty:
                    break
                if event == 'status':
                    self.rotation_data = value
                    if not self.rotation_loaded:
                        self.rotation_selected = [r['label'] if isinstance(r, dict) else r for r in value.get('accounts', [])]
                        self.rotation_loaded = True
                elif event == 'line':
                    self.rotation_errors.append(value)
                elif event == 'done':
                    if value:
                        self.rotation_data = {**self.rotation_data, 'state': 'error',
                                              'detail': 'Could not read rotation status. Refresh to retry.'}
                    self.rotation_worker = None
                    break
        if not self.worker:
            if not self.demo and not self.modal and time.monotonic()-self.last_refresh >= 300:
                self.refresh()
            return
        while True:
            try:
                event, value = self.worker.events.get_nowait()
            except queue.Empty:
                break
            if event == 'line':
                self.logs.append(value)
                self.logs = self.logs[-40:]
            elif event == 'status':
                self.state = value
                self.status_received = True
            elif event == 'done':
                job, self.worker, self.job = self.job, None, None
                if job == 'status':
                    if value or not self.status_received:
                        self.notice = 'Could not refresh Codex accounts. Press r to retry.'
                        return
                    self._advance_setup()
                    if self.onboard:
                        self.onboard = False
                        if not self.state.get('accounts'):
                            self.begin_setup()
                    pending, self.deferred_action = self.deferred_action, None
                    if pending and pending[:3] == (self.page, self.label, (self.wizard or {}).get('stage')):
                        self.dispatch(pending[3])
                elif value:
                    self.message('Action needs attention', self.logs or ['Codex could not complete this action. Your profile is retained.'])
                    self.refresh()
                elif job == 'change-login':
                    self.launch_cli(authenticate=True)
                else:
                    self.notice = ' '.join(self.logs[-2:]) or 'Finished.'
                    if job == 'create':
                        self.wizard = {'stage': 'signin'}
                        self.page, self.cursor = 'wizard', 0
                    elif job in ('remove', 'restore'):
                        self.goto('home' if job == 'remove' else 'removed')
                    elif job == 'rotation-enable':
                        self.rotation_loaded = False
                    self.refresh()
                return

    def _advance_setup(self):
        if self.page == 'wizard' and self.wizard and self.wizard['stage'] == 'signin':
            if (self.account or {}).get('cli_state') in ('ready', 'api_key'):
                self.wizard['stage'] = 'ready'
                self.cursor = 0
                self.notice = 'Existing login verified. CLI and Desktop reuse this account.'

    def begin_setup(self, existing=False):
        self.page, self.cursor = 'wizard', 0
        self.wizard = {'stage': 'signin' if existing else 'components'}
        self.input_text = ''
        self.notice = ''
        self._advance_setup()

    def items(self):
        row = self.account or {}
        if self.page == 'home':
            return [('account:'+a['label'], a.get('email') or a['label'], a.get('cli_detail', '')) for a in self.state.get('accounts', [])] + [
                ('add', 'Add new account', 'Choose CLI, Desktop, or both; sign in once'),
                ('refresh', 'Refresh accounts', 'Check current Codex logins and usage'),
                ('tools', 'Tools', 'Auto rotation, shared history, and removed profiles'),
                ('providers', 'Back to provider selection', 'Manage Claude or Codex'),
                ('quit', 'Quit', '')]
        if self.page == 'account':
            rows = []
            if row.get('cli_state') == 'external':
                rows += [('change-login', 'Set up managed login', 'Sign in once for this profile; keep the existing app login untouched')]
            elif row.get('cli_state') == 'drift':
                rows += [('change-login', 'Reconcile profile login', 'Archive the changed login and sign into the account for this profile')]
            elif row.get('cli_state') not in ('ready', 'api_key'):
                rows += [('setup', 'Finish account setup', 'Verify an existing login or sign in once')]
            if 'desktop' in self.components and row.get('cli_state') in ('ready', 'api_key') and not self.desktop_uses_account(row):
                rows += [('switch', 'Open Codex Desktop' if row.get('desktop_active') else 'Use this account in Desktop',
                          'Reuse the saved login and shared local history')]
            if 'cli' in self.components:
                rows += [('cli', 'Open Codex CLI', 'Use /exit to return; local history stays shared')]
            rows += [('verify', 'Refresh and verify login', 'Try official token renewal before browser sign-in'),
                     ('usage', 'Usage and reset times', 'Show all reported Codex usage windows'),
                     ('manage', 'Manage profile', 'Change the shared login or remove this profile'),
                     ('back', 'Back to accounts', '')]
            return rows
        if self.page == 'manage':
            return [('change-login', 'Change CLI + Desktop login', 'Archive the old login, then sign into the replacement account'),
                    ('save', 'Use current Desktop login', 'Import the existing signed-in account; no second sign-in'),
                    ('remove', 'Remove profile from manager', 'Keep credentials and local history; restore later'),
                    ('back', 'Back to profile', '')]
        if self.page == 'tools':
            rows = [('rotation', 'Auto rotation', 'Choose valid CLI accounts; checks every 5 minutes'),
                    ('sync', 'Import local Codex history', 'Bring native local threads into the shared history store')]
            if self.state.get('recovery_pending'):
                rows.append(('recover', 'Recover interrupted switch', 'Restore the saved state of the interrupted operation'))
            return rows + [('removed', 'Removed profiles', 'Restore retained profiles'), ('back', 'Back to accounts', '')]
        if self.page == 'removed':
            return [('restore:'+(r['label'] if isinstance(r, dict) else r), 'Restore '+(r['label'] if isinstance(r, dict) else r), '')
                    for r in self.state.get('removed_profiles', [])] + [('back', 'Back to tools', '')]
        if self.page == 'rotation':
            self.prune_rotation_selection()
            status = self.rotation_status()
            rows = [('rotate_pick:'+r['label'], r.get('email') or r['label'], 'Space toggles this account.') for r in self.rotation_accounts()]
            saved = [r['label'] if isinstance(r, dict) else r for r in status.get('accounts', [])]
            if not status.get('enabled') or saved != self.rotation_selected:
                rows.append(('rotate_enable', 'Save selection'+('' if status.get('enabled') else ' and enable rotation'), 'Select at least two valid CLI logins'))
            if status.get('enabled'):
                rows += [('rotate_check', 'Check quotas now', 'Refresh quota status while respecting server backoff'),
                         ('rotate_disable', 'Disable auto rotation', 'Keep shared history and the last selected login')]
            return rows + [('rotate_help', 'Rotation and shared history', 'Read how running clients behave during rotation'),
                           ('back', 'Back to tools', '')]
        if self.page == 'wizard':
            stage = self.wizard['stage']
            if stage == 'components':
                return [('component:'+v, ('[x] ' if v in self.components else '[ ] ')+('Codex CLI' if v == 'cli' else 'Codex Desktop')+
                         ('  ✓ Detected' if self.detected[v] else '  Not installed'), 'Space toggles this choice') for v in COMPONENTS] + [
                         ('components_done', 'Continue', 'Sign-in is shared by CLI and Desktop'), ('back', 'Cancel setup', '')]
            if stage == 'name':
                return []
            if stage == 'signin':
                return [('login', 'Sign in with ChatGPT', 'Use the official Codex browser sign-in flow'),
                        ('save', 'Use current Desktop login', 'Reuse an existing Codex Desktop login'),
                        ('verify', 'Check existing login', 'Refresh and verify without opening the CLI'),
                        ('back', 'Pause setup', 'Your profile is retained')]
            if stage == 'ready':
                rows = []
                if 'desktop' in self.components and not self.desktop_uses_account(row):
                    rows.append(('switch', 'Open Desktop with this account', 'Use the same login and local threads'))
                if 'cli' in self.components:
                    rows.append(('cli', 'Open Codex CLI', 'Continue a shared local thread'))
                return rows + [('done', 'Done — back to accounts', '')]
        return []

    def dispatch(self, action):
        if action.startswith('account:'):
            self.label = action.split(':', 1)[1]
            self.goto('account')
        elif action == 'providers':
            self.back_to_providers, self.running = True, False
        elif action == 'quit':
            self.running = False
        elif action == 'add':
            self.label = None
            self.begin_setup()
        elif action == 'setup':
            self.begin_setup(existing=True)
        elif action in ('tools', 'manage', 'removed'):
            self.goto(action)
        elif action == 'rotation':
            self.goto('rotation')
            self.refresh()
        elif action in ('back', 'done'):
            destination = {'account': 'home', 'manage': 'account', 'removed': 'tools', 'rotation': 'tools', 'tools': 'home'}.get(self.page, 'home')
            self.wizard = None
            self.goto(destination)
        elif action == 'refresh':
            self.refresh()
        elif action.startswith('component:'):
            value = action.split(':', 1)[1]
            if value in self.components:
                self.components.remove(value)
            else:
                self.components.append(value)
        elif action == 'components_done':
            if not self.components:
                self.notice = 'Select CLI, Desktop, or both with Space.'
                return
            if not self.demo:
                save_components(self.components)
            self.wizard['stage'], self.cursor = 'name', 0
        elif action.startswith('rotate_pick:'):
            label = action.split(':', 1)[1]
            self.prune_rotation_selection()
            if label not in {a['label'] for a in self.rotation_accounts()}:
                return
            if label in self.rotation_selected:
                self.rotation_selected.remove(label)
            else:
                self.rotation_selected.append(label)
        elif action == 'rotate_enable':
            self.prune_rotation_selection()
            if len(self.rotation_selected) < 2:
                self.message('Select accounts', ['Select at least two valid CLI accounts with Space.'])
                return
            self.confirm(action, 'Enable Codex rotation?', [
                'Selected accounts: '+', '.join(self.rotation_selected),
                'Check quotas every 5 minutes and switch when the active account cannot continue.',
                'New CLI processes use the selected account. Running clients keep their login; switching is deferred until idle.',
                'Shared local threads can be resumed. Cloud history stays with its original account.'])
        elif action in ('rotate_disable', 'rotate_check', 'verify') or action.startswith('restore:'):
            self.start(action)
        elif action == 'rotate_help':
            self.message('Codex rotation and history', [
                'Use codex-rotate for the managed shared Codex home.',
                'Checks run every 5 minutes, subject to service backoff. Only valid CLI accounts can participate.',
                'Codex keeps authentication in memory. A running client does not automatically adopt a different login from disk.',
                'Switching is deferred until the managed client is idle. Start a new client and resume the same local thread.',
                'Local threads and their history are shared. Account-bound cloud conversations are not transferred.'])
        elif action in ('cli', 'login'):
            self.launch_cli(authenticate=action == 'login')
        elif action == 'usage':
            usage = (self.account or {}).get('usage', {})
            lines = ['Percentages show usage consumed, not remaining.']
            for window in usage.get('windows', []):
                reset = window.get('resets_at')
                text = time.strftime('%a %b %d, %H:%M %Z', time.localtime(reset)) if reset else 'not provided'
                lines.append(f'{duration_label(window)}: {window.get("percent", 0):g}% used. Resets {text} ({account_usage.countdown(reset)}).')
            lines += [usage.get('detail') or ('Usage is current.' if usage.get('state') == 'ready' else 'Usage is not currently available.')]
            self.message('Codex usage', lines)
        elif action == 'switch':
            if self.desktop_uses_account(self.account or {}):
                self.message('Already active', ['Desktop already uses this account.'])
                return
            self.confirm(action, 'Use this account in Codex Desktop?', [
                'Open or restart the managed Codex Desktop with this profile’s existing login.',
                'Managed Desktop will quit and reopen if it is running. Finish its work before switching. Any unmanaged Desktop stays open.',
                'Local threads and history remain shared. Cloud conversations remain account-bound.'])
        elif action in ('save', 'change-login', 'remove', 'sync', 'recover'):
            titles = {'save': 'Reuse current Desktop login?', 'change-login': 'Change the shared Codex login?',
                      'remove': 'Remove this profile?', 'sync': 'Import local Codex history?', 'recover': 'Recover the interrupted operation?'}
            details = {'save': ['Use the current Codex Desktop account for this profile. CLI and Desktop reuse one login.'],
                       'change-login': ['Keep a backup of the old login, then open official sign-in for the replacement account.',
                                        'This changes the login shared by CLI and Desktop for this profile. Local history is retained.'],
                       'remove': ['Hide this profile from the manager and rotation. Its credentials and local history are retained.',
                                  'You can restore it from Tools → Removed profiles.'],
                       'sync': ['Import native local Codex threads into the managed shared history store.', 'Cloud conversations are not copied between accounts.'],
                       'recover': ['Restore the saved state of the interrupted account operation.']}
            self.confirm(action, titles[action], details[action])

    def start(self, action):
        if self.demo:
            self.message('Demo mode', ['Actions are disabled. No Codex accounts or files were changed.'])
            return
        if self.worker:
            return
        if action.startswith('rotate_'):
            mode = {'rotate_enable': 'enable', 'rotate_disable': 'disable', 'rotate_check': 'tick'}[action]
            arguments = ['--'+mode]
            if mode == 'enable':
                self.prune_rotation_selection()
                if len(self.rotation_selected) < 2:
                    self.message('Select accounts', ['Select at least two valid CLI accounts with Space.'])
                    return
                arguments += self.rotation_selected
            command, job = runtime.command(ROTATION, *arguments), 'rotation-'+mode
        else:
            label = action.split(':', 1)[1] if action.startswith('restore:') else self.label
            action = 'restore' if action.startswith('restore:') else action
            arguments = ['--'+action] + ([label] if action not in ('sync', 'recover') else [])
            command, job = runtime.command(HELPER, *arguments), action
        self.logs = []
        self.worker, self.job = Worker(command), job

    def launch_cli(self, authenticate=False):
        if self.demo:
            self.message('Demo mode', ['CLI and sign-in actions are disabled.'])
            return
        command = runtime.command(HELPER, '--login' if authenticate else '--launch-cli', self.label)
        command = runtime.interactive_command(command)
        result = 1
        curses.def_prog_mode()
        curses.endwin()
        try:
            result = subprocess.run(command, check=False).returncode
            if result not in (0, 130):
                print(f'\nCodex exited with status {result}. Review the CLI message above.', flush=True)
                try:
                    input('Press Enter to return to Codex Accounts… ')
                except EOFError:
                    pass
        except KeyboardInterrupt:
            result = 130
        except OSError:
            result = 1
        finally:
            curses.reset_prog_mode()
            self.screen.clear()
            self.screen.keypad(True)
            self.screen.timeout(100)
            try:
                curses.curs_set(0)
            except curses.error:
                pass
        if result not in (0, 130):
            self.message('Codex stopped', [f'Codex exited with status {result}. Your profile is retained; retry sign-in from here.'])
        self.refresh()

    def header(self):
        row = self.account or {}
        name = row.get('email') or self.label or 'new account'
        if self.page == 'account':
            return 'Accounts / '+name, ['One saved login is reused by CLI and Desktop.', row.get('cli_detail', 'Checking login…')]
        if self.page == 'manage':
            return 'Manage / '+name, ['CLI and Desktop use one canonical login for this profile.']
        if self.page == 'tools':
            return 'Accounts / Tools', ['Manage rotation, shared local history, and saved profiles.']
        if self.page == 'removed':
            return 'Removed profiles', ['Credentials and local history were retained. Choose a profile to restore it.']
        if self.page == 'wizard':
            return {
                'components': ('Add account / apps', ['Choose CLI, Desktop, or both with Space. Sign-in is shared.']),
                'name': ('Add account / label', ['Use 1–32 lowercase letters, numbers, hyphens, or underscores.']),
                'signin': ('Set up / '+str(self.label), ['Sign in once, or reuse the current Desktop login.', 'The same login works for CLI and Desktop.']),
                'ready': ('Account ready / '+name, ['Your Codex login is verified.', 'CLI and Desktop use the same local thread history.'])}[self.wizard['stage']]
        return 'Choose a Codex account', []

    def rotation_panel(self, width, height):
        status = self.rotation_status()
        self.put(3, 2, 'TOOLS / AUTO ROTATION', self.color(1) | curses.A_BOLD, width)
        for line in range(5, 9 if height >= 30 else 8):
            self.fill(line, 2, width, 7)
        health, role = self.rotation_health(status)
        x = self.badge(5, 4, health, role, width+2)
        self.put(5, x, 'Checks every 5 min', self.color(13), width-x)
        active = status.get('active')
        row = next((r for r in self.state.get('accounts', []) if r['label'] == active), {})
        self.put(6, 4, 'SELECTED LOGIN  '+str(row.get('email') or active or 'none'), self.color(25 if active else 13), width-4)
        saved = [r['label'] if isinstance(r, dict) else r for r in status.get('accounts', [])]
        dirty = saved != self.rotation_selected
        self.put(7, 4, f'{len(self.rotation_selected)} accounts selected  •  '+('Unsaved selection' if dirty else 'Selection saved'), self.color(22 if dirty else 13), width-4)
        if height >= 30:
            stamp = status.get('checked_at')
            prefix = 'Last check '+time.strftime('%H:%M', time.localtime(stamp))+': ' if stamp else ''
            self.put(8, 4, prefix+(status.get('detail') or 'New clients use this login; running clients keep theirs.'), self.color(22 if role in (15, 17) else 13), width-4)
        return 10 if height >= 30 else 9

    def account_card(self, y, row, focused, width, rotating=False, compact=False):
        role = 8 if focused else 7
        for offset in range(2 if compact else 4):
            self.fill(y+offset, 2, width, role)
            self.put(y+offset, 2, '▌' if focused else '│', self.color(10 if focused else 18))
        title = ('[x] ' if row['label'] in self.rotation_selected else '[ ] ') if rotating else ''
        state = row.get('cli_state', 'missing')
        name = row.get('email') or ('API key' if state == 'api_key' else 'Existing app login' if state == 'external' else 'Sign-in needed')
        self.put(y, 5, title+'['+row['label']+'] '+name, self.color(role) | curses.A_BOLD, width-4)
        badge, color = ('CLI VALID', 16) if state == 'ready' else ('SIGN-IN NEEDED', 17) if state in ('missing', 'sign_in_required') else ('CHECK LOGIN', 15)
        if state == 'api_key':
            badge, color = 'API KEY', 16
        elif state == 'external':
            badge, color = 'MANAGED LOGIN NEEDED', 15
        elif state == 'drift':
            badge, color = 'LOGIN CHANGED', 15
        if rotating and self.rotation_data.get('active') == row['label']:
            badge, color = 'SELECTED LOGIN', 14
        x = self.badge(y+1, 5, badge, color, width+2)
        if row.get('desktop_active') and not rotating:
            x = self.badge(y+1, x, 'DESKTOP ACTIVE' if self.desktop_uses_account(row) else 'DESKTOP SELECTED', 14, width+2)
        self.put(y+1, x, ('Included in rotation' if row['label'] in self.rotation_selected else 'Not selected') if rotating else 'One login • CLI + Desktop', self.color(9 if focused else 13), width-x)
        if compact:
            return
        monitored = next((r for r in self.rotation_data.get('profiles', []) if r.get('label') == row['label']), {}) if rotating else {}
        usage = monitored.get('usage') or row.get('usage') or {}
        windows = usage.get('windows', [])
        for offset, window in enumerate(windows[:2], 2):
            percent = window.get('percent', 0)
            filled = min(10, max(0, round(percent/10)))
            bar = duration_label(window)[:12]+' ['+'█'*filled+'░'*(10-filled)+f'] {percent:g}% used'
            warning = usage.get('state') != 'ready' or percent >= 80
            color = (28 if focused else 22) if warning else (27 if focused else 25)
            self.put(y+offset, 5, bar, self.color(color), width-4)
            reset = window.get('resets_at')
            detail = ('STALE • ' if usage.get('state') != 'ready' else '')+'resets '+account_usage.countdown(reset)
            self.put(y+offset, 7+len(bar), detail, self.color(9 if focused else 13), width-len(bar)-6)
        if not windows:
            self.put(y+2, 5, 'Usage not available yet', self.color(9 if focused else 13), width-4)
            self.put(y+3, 5, usage.get('detail') or row.get('cli_detail', 'Verify the account to load usage.'), self.color(9 if focused else 13), width-4)

    def draw(self):
        self.screen.erase()
        height, columns = self.screen.getmaxyx()
        width = min(columns-4, 100)
        self.fill(1, 2, max(0, width), 7)
        self.put(1, 4, 'CODEX ACCOUNTS'+('  DEMO' if self.demo else ''), self.color(18) | curses.A_BOLD)
        if height < 20 or columns < 60:
            self.put(3, 2, 'Enlarge terminal to 60 columns × 20 rows.', self.color(4))
            self.put(5, 2, 'Esc: back   q: quit when idle', self.color(1))
            self.screen.refresh()
            return
        if self.page == 'rotation':
            y = self.rotation_panel(width, height)
        else:
            desktop = self.state.get('active_email') or ('Open outside account manager' if self.state.get('desktop_other_running') else 'No managed account selected')
            self.put(3, 2, 'DESKTOP  '+str(desktop), self.color(2 if self.state.get('active_email') else 4), width)
            title, description = self.header()
            self.put(5, 2, title, self.color(1) | curses.A_BOLD, width)
            y = 7
            if self.page == 'home' and not self.state.get('accounts'):
                description = ['Welcome. Add your first Codex account; sign in once for CLI and Desktop.']
            budget = max(1, height-y-9)
            lines = [part for line in description for part in textwrap.wrap(line, width)]
            for line in lines[:budget]:
                self.put(y, 2, line, self.color(4), width)
                y += 1
            if lines:
                y += 1
        footer = '↑/↓ move   Enter: select   Esc: back'
        if self.page == 'wizard' and self.wizard['stage'] == 'name':
            self.fill(y, 2, width, 8)
            self.put(y, 4, 'Label: '+self.input_text+'_', self.color(8) | curses.A_BOLD, width-4)
            footer = 'Type a label   Enter: create   Esc: cancel'
            detail = ''
        else:
            rows = self.items()
            self.cursor = min(self.cursor, max(0, len(rows)-1))
            def layout(start):
                blocks, position, previous = [], y, None
                for i in range(start, len(rows)):
                    action = rows[i][0]
                    account = action.startswith(('account:', 'rotate_pick:'))
                    group = ('ACCOUNTS • SPACE TO SELECT' if self.page == 'rotation' else 'ACCOUNTS') if account else 'ACTIONS'
                    heading = self.page in ('home', 'rotation') and previous != group
                    size = (3 if height < 30 else 5) if account else 1
                    if position+size+int(heading) > height-5:
                        break
                    if heading:
                        blocks.append(('heading', position, group))
                        position += 1
                    blocks.append(('item', position, i))
                    position += size
                    previous = group
                return blocks
            start = 0
            blocks = layout(start)
            while rows and not any(kind == 'item' and index == self.cursor for kind, _, index in blocks):
                start += 1
                blocks = layout(start)
                if start >= len(rows):
                    break
            for kind, line, index in blocks:
                if kind == 'heading':
                    self.rule(line, index, width)
                    continue
                action, name, _ = rows[index]
                selected = index == self.cursor
                if action.startswith(('account:', 'rotate_pick:')):
                    row = next(r for r in self.state['accounts'] if r['label'] == action.split(':', 1)[1])
                    self.account_card(line, row, selected, width, rotating=action.startswith('rotate_pick:'), compact=height < 30)
                else:
                    self.fill(line, 2, width, 8 if selected else 7)
                    self.put(line, 3, '›' if selected else '‹' if action in ('back', 'providers') else '·', self.color(10 if selected else 19))
                    self.put(line, 6, name, self.color(8 if selected else 19 if action in ('add', 'rotate_enable') else 7) | (curses.A_BOLD if selected else 0), width-5)
            detail = rows[self.cursor][2] if rows else ''
            if self.page == 'rotation' or (self.page == 'wizard' and self.wizard['stage'] == 'components'):
                footer = '↑/↓ move   Space: toggle   Enter: select   Esc: back'
        message = ('Working… '+(self.logs[-1] if self.logs else '')) if self.worker else self.notice or detail
        self.put(height-4, 2, message, self.color(3 if self.notice or self.worker else 4), width)
        self.rule(height-3, '', width)
        self.put(height-2, 2, footer, self.color(1), width)
        if self.modal:
            self._modal_paint = True
            try:
                self.draw_modal()
            finally:
                self._modal_paint = False
        self.screen.refresh()

    def key(self, key):
        if self.worker:
            if self.job == 'status' and not self.modal and not (self.page == 'wizard' and self.wizard['stage'] == 'name'):
                rows = self.items()
                if key in (curses.KEY_UP, ord('k'), curses.KEY_DOWN, ord('j')):
                    self.cursor = (self.cursor+(-1 if key in (curses.KEY_UP, ord('k')) else 1)) % max(1, len(rows))
                    self.deferred_action = None
                elif key in (10, 13, curses.KEY_ENTER) and rows:
                    self.deferred_action = (self.page, self.label, (self.wizard or {}).get('stage'), rows[self.cursor][0])
                    self.notice = 'Checking accounts; your selection will open when ready.'
            return
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
            return
        if self.page == 'wizard' and self.wizard['stage'] == 'name':
            if key == 27:
                self.wizard['stage'], self.cursor = 'components', 0
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                self.input_text = self.input_text[:-1]
            elif key in (10, 13, curses.KEY_ENTER):
                if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}', self.input_text):
                    self.notice = 'Use 1–32 lowercase letters, numbers, hyphens, or underscores.'
                elif self.input_text in [r['label'] for r in self.state.get('accounts', [])]:
                    self.notice = 'That profile exists. Open it from Accounts to continue setup.'
                else:
                    self.label = self.input_text
                    self.start('create')
            elif 32 <= key < 127 and len(self.input_text) < 32:
                self.input_text += chr(key)
            return
        rows = self.items()
        if key in (curses.KEY_UP, ord('k'), curses.KEY_DOWN, ord('j')):
            self.cursor = (self.cursor+(-1 if key in (curses.KEY_UP, ord('k')) else 1)) % max(1, len(rows))
        elif key == 32 and rows and rows[self.cursor][0].startswith(('rotate_pick:', 'component:')):
            self.dispatch(rows[self.cursor][0])
        elif key in (10, 13, curses.KEY_ENTER) and rows:
            self.dispatch(rows[self.cursor][0])
        elif key == 27:
            self.dispatch('providers' if self.page == 'home' else 'back')
        elif key == ord('q'):
            self.running = False
        elif key == ord('r'):
            self.refresh()

    def run(self):
        result = super().run()
        return BACK_TO_PROVIDERS if result == 0 and self.back_to_providers else result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--onboard', action='store_true')
    parser.add_argument('--cli', metavar='LABEL')
    parser.add_argument('cli_args', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.cli:
        if args.demo:
            parser.error('--cli cannot be combined with --demo')
        extra = args.cli_args[1:] if args.cli_args[:1] == ['--'] else args.cli_args
        command = runtime.command(HELPER, '--launch-cli', args.cli, '--', *extra)
        if sys.stdin.isatty() and sys.stdout.isatty():
            command = runtime.interactive_command(command)
        return subprocess.call(command)
    if args.cli_args:
        parser.error('CLI options require --cli LABEL')
    if sys.platform != 'darwin' and not args.demo:
        parser.error('Desktop account management currently supports macOS only.')
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print('Open Codex Accounts in an interactive terminal.', file=sys.stderr)
        return 2
    try:
        return curses.wrapper(lambda screen: App(screen, args.demo, args.onboard).run()) or 0
    except KeyboardInterrupt:
        return 130
    except curses.error:
        print('The terminal could not be initialized. Use TERM=xterm-256color.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    runtime.run_main(main)
