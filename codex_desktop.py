"""Codex Desktop discovery and instance-scoped lifecycle on macOS.

A shared CODEX_HOME preserves local Codex threads. Cloud ChatGPT conversations
and account-scoped services remain owned by their original account.
"""
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import subprocess
import time

import codex_install


BUNDLE_ID = 'com.openai.codex'


def _app_info(path):
    try:
        info = plistlib.loads((path/'Contents/Info.plist').read_bytes())
        identifier = info.get('CFBundleIdentifier', '')
        executable = info.get('CFBundleExecutable', '')
        if identifier != BUNDLE_ID and not identifier.startswith(BUNDLE_ID+'.'):
            return None
        if not executable or Path(executable).name != executable:
            return None
        binary = path/'Contents/MacOS'/executable
        if binary.is_file():
            return info
    except (OSError, ValueError, plistlib.InvalidFileException):
        pass
    return None


def installed_app(home=None):
    """Find by bundle identity, including the renamed ChatGPT.app distribution."""
    home = Path.home() if home is None else Path(home)
    for root in (home/'Applications', Path('/Applications')):
        preferred = [root/'Codex.app', root/'ChatGPT.app']
        candidates = preferred + sorted(p for p in root.glob('*.app') if p not in preferred)
        for path in candidates:
            if _app_info(path):
                return path
    return None


def _processes():
    result = subprocess.run(['ps', '-axo', 'pid=,ppid=,command='], capture_output=True,
                            text=True, timeout=5, check=True)
    processes = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            processes[int(parts[0])] = {'ppid': int(parts[1]), 'command': parts[2]}
    return processes


def _descendants(processes, parents):
    found = set(parents)
    while True:
        added = {pid for pid, row in processes.items() if row['ppid'] in found} - found
        if not added:
            return found-set(parents)
        found.update(added)


def _has_user_data(command, user_data):
    # ps does not quote arguments containing spaces. Electron emits another
    # option after this path, or it is the final argument passed by our launcher.
    value = re.escape(str(Path(user_data).resolve()))
    return bool(re.search(r'(?:^|\s)--user-data-dir(?:=|\s)["\']?'+value+r'["\']?(?=\s--|$)', command))


def _uses_runtime(pid, runtime_home):
    """Prove an orphan helper uses our home without reading process secrets."""
    try:
        result = subprocess.run(['lsof', '-a', '-p', str(pid), '-d', '0-999', '-Fn'],
                                capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return False
    prefix = str(Path(runtime_home).resolve())+'/'
    return any(line.startswith('n'+prefix) for line in result.stdout.splitlines())


def _external_runtime_pids(processes, runtime_home, excluded=()):
    if runtime_home is None:
        return []
    # Standalone CLI/app-server clients do not hold this manager's lease. Native
    # Codex keeps its database open, which lets us identify the shared home
    # without inspecting environment variables, tokens or prompt contents.
    candidates = {pid for pid, row in processes.items() if pid != os.getpid()
                  and re.match(r'^(?:.*[/\\])?codex(?:-[A-Za-z0-9_.-]+)?(?:\s|$)', row['command'])}
    return sorted(pid for pid in candidates-set(excluded) if _uses_runtime(pid, runtime_home))


def _classify(processes, app, runtime_home=None, user_data=None):
    info = _app_info(app)
    if not info:
        raise RuntimeError('Codex Desktop is not installed or its application bundle is invalid.')
    main = str(app/'Contents/MacOS'/info['CFBundleExecutable'])
    prefix = str(app/'Contents')+'/'
    all_main = {pid for pid, row in processes.items()
                if row['command'] == main or row['command'].startswith(main+' ')}
    selected = all_main if user_data is None else {
        pid for pid in all_main if _has_user_data(processes[pid]['command'], user_data)}
    descendants = _descendants(processes, selected)
    other_descendants = _descendants(processes, all_main-selected)
    bundle_helpers = {pid for pid, row in processes.items()
                      if pid not in all_main and row['command'].startswith(prefix)}
    known = bundle_helpers & descendants
    unknown = set()
    for pid in bundle_helpers-descendants-other_descendants:
        command = processes[pid]['command']
        if user_data is not None and _has_user_data(command, user_data):
            known.add(pid)
        elif (' app-server' in command or
              (runtime_home is not None and _uses_runtime(pid, runtime_home))):
            # An outside client can launch the bundled app-server against our
            # home. Open runtime files prove it is busy, not GUI ownership.
            unknown.add(pid)
    external = _external_runtime_pids(processes, runtime_home, all_main | bundle_helpers)
    return {'installed': True, 'app': str(app), 'running': bool(selected),
            'background': bool(known or unknown or external), 'busy': bool(selected or known or unknown or external),
            'managed': user_data is not None and bool(selected or known),
            'any_running': bool(all_main), 'pids': sorted(selected),
            'helper_pids': sorted(known), 'unknown_background': bool(unknown),
            'unknown_helper_pids': sorted(unknown), 'external_runtime_pids': external}


def desktop_state(runtime_home=None, user_data=None, app=None):
    app = Path(app) if app is not None else installed_app()
    try:
        processes = _processes()
        if app is None:
            external = _external_runtime_pids(processes, runtime_home)
            return {'installed': False, 'app': None, 'running': False, 'background': bool(external),
                    'busy': bool(external), 'managed': False, 'any_running': False, 'pids': [],
                    'helper_pids': [], 'unknown_background': False, 'unknown_helper_pids': [],
                    'external_runtime_pids': external}
        return _classify(processes, app, runtime_home, user_data)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError('Cannot check Codex Desktop processes; no account switch was performed.') from error


def ensure_supported(app):
    """Older bundled cores may replace the canonical auth symlink on refresh."""
    executable = codex_install.bundled_cli(app)
    supported = False
    if executable:
        try:
            result = subprocess.run([str(executable), '--version'], capture_output=True,
                                    text=True, timeout=10)
            match = re.search(r'\bcodex-cli\s+(\d+)\.(\d+)\.(\d+)', result.stdout)
            supported = result.returncode == 0 and match and tuple(map(int, match.groups())) >= (0, 166, 0)
        except (OSError, subprocess.SubprocessError):
            pass
    if not supported:
        raise RuntimeError('Update Codex Desktop to a build with Codex CLI 0.166.0 or newer before opening managed accounts.')
    return executable


def launch_app(runtime_home, user_data, app=None):
    app = Path(app) if app is not None else installed_app()
    if app is None or not _app_info(app):
        raise RuntimeError('Install Codex Desktop first.')
    ensure_supported(app)
    runtime_home, user_data = Path(runtime_home).resolve(), Path(user_data).resolve()
    for path in (runtime_home, user_data):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Both variables are required: Desktop's login-shell environment loader
    # explicitly preserves CODEX_HOME when an explicit Electron home is present.
    subprocess.run(['/usr/bin/open', '-n', '--env', 'CODEX_HOME='+str(runtime_home),
                    '--env', 'CODEX_ELECTRON_USER_DATA_PATH='+str(user_data), str(app),
                    '--args', '--user-data-dir='+str(user_data)], check=True, timeout=30)


def quit_app(runtime_home=None, user_data=None, app=None, timeout=15):
    """Gracefully stop only the selected GUI instances and their proven helpers.

    Never uses killall/pkill: ordinary CLI tasks and other Desktop instances
    remain outside the selected process set. Unattributed orphan app servers
    cause a refusal instead of guessing at their ownership.
    """
    app = Path(app) if app is not None else installed_app()
    if app is None:
        return
    processes = _processes()
    state = _classify(processes, app, runtime_home, user_data)
    if state['external_runtime_pids']:
        raise RuntimeError('A separate Codex CLI or app-server is using the shared runtime. Close that client (including T3Code), then retry; it will not be terminated automatically.')
    main_pids = set(state['pids'])
    owned = {pid: processes[pid]['command'] for pid in state['helper_pids']}
    # NSRunningApplication targets a PID. Quitting by bundle ID is ambiguous
    # when the default and managed Codex instances are both open.
    for pid in main_pids:
        identifier = _app_info(app)['CFBundleIdentifier']
        script = ("ObjC.import('AppKit'); "
                  f"var app = $.NSRunningApplication.runningApplicationWithProcessIdentifier({pid}); "
                  "if (!app.isNil()) { "
                  f"if (ObjC.unwrap(app.bundleIdentifier) !== {json.dumps(identifier)}) "
                  "throw new Error('The selected process changed'); "
                  "if (!app.terminate) throw new Error('Codex declined to quit'); }")
        subprocess.run(['/usr/bin/osascript', '-l', 'JavaScript', '-e', script],
                       capture_output=True, text=True, check=True, timeout=10)
    deadline = time.monotonic()+timeout
    while True:
        processes = _processes()
        remaining = main_pids & set(processes)
        for pid in _descendants(processes, remaining):
            command = processes[pid]['command']
            if command.startswith(str(app/'Contents')+'/'):
                owned[pid] = command
        if not remaining:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError('Codex Desktop has not finished quitting. Finish its active work and retry.')
        time.sleep(.1)
    for pid, command in owned.items():
        if processes.get(pid, {}).get('command') == command:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic()+5
    while True:
        state = desktop_state(runtime_home, user_data, app)
        if not state['busy']:
            return
        if state['external_runtime_pids']:
            raise RuntimeError('A separate Codex CLI or app-server is using the shared runtime. Close that client (including T3Code), then retry; it will not be terminated automatically.')
        if state['unknown_background']:
            raise RuntimeError('A Codex app server is still running and its profile cannot be identified. No login files were changed.')
        if time.monotonic() >= deadline:
            raise RuntimeError('Codex Desktop background tasks are still stopping. Retry after they finish; no login files were changed.')
        time.sleep(.1)
