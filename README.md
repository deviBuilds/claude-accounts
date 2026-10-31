# Claude Accounts (macOS)

A local terminal interface for managing multiple Claude Code subscription logins
and saved Claude Desktop profiles. Account identities are discovered and verified;
no developer account, email, token, or Desktop snapshot ships with this software.

## One-command install (no Python setup)

Run in Terminal:

```sh
curl -fsSL https://raw.githubusercontent.com/deviBuilds/claude-accounts/main/install.sh | bash
```

The small installer downloads the versioned release, verifies its SHA-256 checksum,
and reconnects to your terminal for interactive setup.
You can also download the [standalone installer or executable](https://github.com/deviBuilds/claude-accounts/releases/latest) manually.

The current binary release is for **Apple Silicon Macs running macOS 13+**.
The executable includes its Python runtime, certificates and modules. End users do
not install Python, pip, Node.js, Homebrew, Rust, or build tools. This is a bundled
executable of the tested Python implementation, not a Rust rewrite.

The installer:

1. Checks your Mac and verifies the executable's checksum. Then shows a checklist:
   **↑/↓** to move, **Space** to select CLI, Desktop, or both, **Enter** to continue.
   Installed apps show **✓ Detected** and are reused; installation detection does
   not imply you are signed in. You can deselect a detected app.
2. Reuses existing Claude Code and Desktop installations.
3. If selected, installs a missing CLI with [Anthropic's native installer](https://code.claude.com/docs/en/setup).
4. If selected, downloads a missing Desktop directly from Anthropic, using the URL and checksum
   in [Homebrew's public cask metadata](https://formulae.brew.sh/cask/claude). It
   verifies the archive checksum and Anthropic's code signature before copying the
   app to `/Applications`. Homebrew itself is not installed. macOS may request an
   administrator password for that copy.
5. Saves your choices and installs `~/.local/bin/claude-accounts` and the compatible `claude-desktop-sync`
   launcher, then opens setup for a new user. Existing users see their accounts.

Only selected apps are installed and onboarded. CLI-only setup ends after CLI
verification. Desktop-only setup checks and saves the encrypted Desktop login
without installing the CLI or creating a CLI profile. Browser sign-ins are
completed by the user. The installer does not
edit shell configuration or replace saved accounts. Rerunning it skips installed
dependencies. Failures stop with an error; they do not open unfinished onboarding.
A broken existing CLI is reported rather than silently replaced.

To check dependencies without changing anything:

```sh
curl -fsSL https://raw.githubusercontent.com/deviBuilds/claude-accounts/main/install.sh | bash -s -- --check
```

To change your app choices later, run `claude-accounts --install` again.
For scripted installation, append `--components=cli`, `--components=desktop`, or
`--components=both` after `bash -s --`. With `--no-launch`, omitted choices reuse
your saved selection (both on a first install).

For installation without opening the TUI, use `bash -s -- --no-launch` at the end
of the command. Later, run
`~/.local/bin/claude-accounts`; add `~/.local/bin` to PATH for the shorter command.

The installer file contains the program, so it can be shared directly. Only missing
Claude dependencies require downloads when using that self-contained file.
The curl command downloads the program from this repository's GitHub Release assets.
The binary is ad-hoc signed for local execution, not Apple-notarized. Gatekeeper or
managed-device policy may require the user's approval. An Intel binary must be built
and tested separately on Intel; it is not included in this release.

### Source installation

Developers can still unzip the source bundle and run `python3 install.py`. This path
requires Python 3.10+ and separately installed Claude Code and Desktop. The source
installer writes modules to `~/.local/lib/claude-accounts` and shell launchers to
`~/.local/bin`.

## First account

1. The standalone installer opens setup automatically for a new user. When opening
   the manager yourself, choose **Set up your first account**. The default CLI
   profile is named `a`.
2. If CLI was selected, choose **Open CLI to sign in**, use `/login`, and then
   `/exit` to return to setup. A separate browser-login command is also available;
   if it fails, the manager offers the interactive CLI without restarting. CLI launch
   uses a fresh terminal connection, including immediately after the curl installer.
   On a CLI error, its original output stays visible until you press Enter; no
   sign-in transcript is recorded. CLI-only
   setup finishes here; the manager verifies its identity.
3. If Desktop was selected, choose **Open Desktop sign-in** and sign into the
   intended account. With both apps selected, use the same account in each.
   A first Desktop launch initializes its own data directory.
4. When the matching login is detected, choose **Save Desktop login and finish**;
   otherwise choose **Verify and finish setup** after signing in. If running,
   Desktop closes gracefully to flush its login,
   the manager verifies the encrypted login, saves its profile, and reopens it.
   Saving checks the live process state first: an open app gets a **Close Desktop
   and save** confirmation. If only background tasks remain, choose **Stop tasks
   and save**. The manager stops Desktop tasks automatically, first requesting normal
   termination and then force-stopping tasks that do not exit. It waits for them to
   stop before saving; unrelated standalone CLI sessions and crash reporters are
   left alone. A closed app stays closed. If shutdown is denied, the error explains
   the cause and setup progress is kept for retrying.
   With both apps selected, it also checks that the CLI and Desktop identities match.
   Desktop-only profiles display your chosen label because a cookie verifies the
   account ID but does not provide an email address.

If Claude Code is already signed in, that account appears automatically. Choose
**Set up Desktop login**, or **Save current Desktop login** if Desktop already uses it.
If the current Desktop account has no matching CLI profile, its verified local
login is saved under a generated `desktop-…` label before switching away. Existing
labels are never reassigned. That account remains available for switching back.

If Desktop setup returns to the wrong signed-in account, choose **Open Desktop
sign-in** again. The manager saves that outgoing login and prepares a fresh
profile instead of repeatedly reopening it. If browser sign-in automatically
chooses the wrong account, select the intended account before approving.
An unfinished, unsigned browser flow is preserved when resuming.

For more accounts choose **Add new account**, pick a label such as `work`, and follow
the same sign-in flow. You can pause and resume. Desktop and CLI require separate
initial authorization; a CLI token cannot log you into Desktop.

## Daily use

Use Up/Down, Enter and Esc. Switching Desktop saves the outgoing account before
restoring the target. The active account does not offer a redundant Switch action.
Expired CLI access tokens get one background renewal attempt while the TUI is open;
that attempt uses a small model request and can consume usage. During onboarding,
a verified existing or renewed CLI login automatically advances setup; the CLI
sign-in actions are hidden while renewal is in progress. A genuinely expired
or revoked login still requires browser sign-in. You can retry network failures.

Account cards show 5-hour and weekly percentages **used**, local reset times and
countdowns. **View usage limits** shows all available model-specific windows and the
last successful update. Usage percentages and automatic CLI renewal require a
CLI login and are unavailable in Desktop-only mode. Usage is cached for five minutes; errors and stale readings
are labeled. A reset passing does not imply a freshly confirmed 0% reading.

### Manage and reconcile profiles

Open an account → **Manage profile** to change its CLI login, change its Desktop
login, or remove it from the manager.

- **Change CLI login** opens the official sign-in flow for that profile. Its
  Desktop login is retained. Choose the intended account in the browser.
- **Change Desktop login** preserves the outgoing login and opens a clean Desktop
  sign-in. Choose **Save new Desktop login** afterward. The previous saved login
  remains available until the replacement is saved, and its snapshot is retained.
  A cancelled change leaves the current app and saved logins intact.
- **Remove profile from manager** hides the entry without signing either app out
  or deleting Keychain credentials, CLI settings, shared history, or snapshots.
  This is reversible removal, not credential deletion. Use **Tools → Removed
  profiles** to restore it. Its label remains reserved until restored, including
  the default profile a. The manager's --cli command rejects removed profiles.

When CLI and Desktop identities differ, the account card shows **LOGIN MISMATCH**
and the account menu offers **Resolve CLI/Desktop mismatch**. Both identities are
shown separately. Choose which to keep:

- **Match saved Desktop logins to CLI accounts** previews all available repairs,
  including swapped profiles. It verifies the saved encrypted logins and updates
  the associations together. CLI credentials and the running Desktop app are
  unchanged. If identities changed after the preview, refresh and review again.
- **Keep CLI account; sign Desktop into it** starts a fresh Desktop sign-in and
  requires the saved result to match that CLI identity.
- **Keep Desktop account; change CLI login** opens official CLI sign-in with the
  saved Desktop email when known. The manager reports any mismatch afterward.

Previous Desktop associations are retained in the local profile index. Credentials
are never copied between CLI profiles. Switching a mismatched profile is blocked
until its identities are reconciled.

Open a CLI profile from any shell without configuring aliases:

```sh
claude-accounts --cli a
claude-accounts --cli work
claude-accounts --cli work -- --continue
```

Additional CLI profiles use `~/.claude-acct-<label>`. Their local settings, projects
and history are shared with `~/.claude`; credentials remain separate. Default profile
a must run with `CLAUDE_CONFIG_DIR` unset. Profiles created elsewhere with unrelated
configuration-directory names are not discovered automatically.

## Storage and limits

- Desktop snapshots and rollback copies: `~/.claude-desktop-profiles`.
- Background refresh attempt metadata: `~/.claude-account-refresh`.
- Usage cache: `~/.claude-account-usage` (percentages and timestamps, no tokens).
- App selection: `~/.config/claude-accounts/preferences.json`.
- OAuth credentials stay in the user's own macOS Keychain.

Snapshots contain private application data and encrypted login cookies. **Share
only the source ZIP or generated executable/installer**, never these local
directories or a copied home directory.
Snapshot restore is for the same Mac and Keychain. Old snapshots are retained and
can occupy disk space. Switches briefly stop Desktop and its running sessions.
Local Code history is merged; server-side chats remain owned by their original
account. Changing accounts does not transfer those cloud conversations.

This is an independent local helper, not an official Anthropic product. Desktop's
profile/cookie format and the subscription usage endpoint are internal interfaces
and may change. Windows and Linux Desktop integration are not implemented. API-key,
Console, and enterprise gateway login flows are not the supported onboarding path.
The current macOS credential reader expects a Keychain-backed subscription login;
Claude Code's file fallback for a locked Keychain is not yet supported.

## Verification

```sh
python3 -m unittest discover -p 'test_*.py'
```

The tests use temporary homes and mocked logins; they never switch a real Desktop
account. `python3 accounts_tui.py --demo` previews the UI with fictional data.

## Build a standalone release

Build tools are needed only on the developer's Mac. Use a Python distribution with
a deployment target compatible with the macOS versions you support; a Homebrew
Python built for a recent macOS can make the result unusable on older systems.
The tested Apple Silicon release uses uv's managed Python 3.13 (macOS deployment
target 11), while the app requires macOS 13+ for Claude dependencies.

```sh
uv venv --python 3.13 .build-venv
uv pip install --python .build-venv/bin/python -r requirements-build.txt
.build-venv/bin/python build_release.py
```

Build on each target architecture. Releases default to
`~/.local/share/claude-accounts/releases`: executable, self-extracting installer,
SHA-256 manifest and source ZIP. The source ZIP uses an explicit file list; account
snapshots, usage caches, credentials and build environment files are excluded.

## Automatic CLI account rotation

Open **Auto rotation** in the TUI. Select at least two distinct signed-in CLI
accounts with **Space**, then choose **Apply selection and enable rotation**.
Rotation is opt-in. It installs a per-user macOS LaunchAgent, runs immediately,
and checks selected accounts every 60 seconds while you are logged in. Sleep,
slow network calls, and server `Retry-After` responses can delay checks.

Use `claude-rotate` in Terminal. For T3Code, configure a Claude provider with:

- **Binary path:** `claude`
- **CLAUDE_CONFIG_DIR:** the absolute path to `~/.claude-rotate`
- Leave `HOME` unchanged and remove provider environment overrides such as
  `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, and `ANTHROPIC_AUTH_TOKEN`.

The shared directory keeps settings, plugins, and local conversations in one
place. It begins as a new configuration; existing account settings and histories
are not automatically copied into it. Conversations in this directory may be
sent under any selected account. Use only accounts authorized for that data.

The monitor keeps the current account while healthy. It tries the original CLI's
background renewal when an access token expires, and chooses the next eligible
selected account if renewal fails or a reported usage window reaches 100%.
Selection follows the order in which profiles were selected. Any reported
model-specific limit conservatively excludes the account, even if another model
could still work; paid extra usage is not used to override an exhausted window.
Network errors and stale usage do not trigger a switch away from the current
account. If all accounts are exhausted, it waits for a reset. No prompt is
replayed automatically.

Only access tokens are copied to the shared directory, with private permissions
and atomic replacement. Refresh tokens remain in the original Keychain profiles.
This prevents the shared profile and source profile from competing to rotate a
refresh token. Ordinary quota polls do not run model prompts; expired-login
renewal uses the existing CLI refresh helper, which may consume a small amount
of usage. Do not use `/login`, `/logout`, or `claude auth login` in the shared
directory. Sign in using the original profile and reapply your selection if its
account identity changed. A conflicting Keychain login in the shared directory
stops the monitor rather than silently using the wrong account.

### Persistent sessions and limits

Tested with Claude Code **2.1.316**: a persistent `stream-json` CLI reloaded an
access-token file between requests. A local mock server observed different
Authorization headers; a live two-account test completed both replies without
restarting the process. This is observed CLI behavior, not a documented stable
credential-reload API. Retest when changing CLI versions.

T3Code's current Claude adapter reuses its process across turns. Shared access
tokens support that lifecycle, but polling cannot guarantee recovery of a request
already in flight or a turn already waiting for quota. Stop that turn and send a
new message if it remains blocked. Automatic model requests and background
subagents can also observe a new token on their next request; rotation is not
restricted to UI reply boundaries. T3Code may retain a cached account label until
its provider refreshes. No T3Code application code is modified by this feature.

**Disable rotation** removes the LaunchAgent and stops checks. It does not kill
running CLI processes, delete shared conversations, or revoke the last access
token. Existing processes, or T3Code pointed directly at the shared directory,
can continue using that token until it expires. The `claude-rotate` launcher
requires rotation to be enabled.

For diagnostics:

```sh
claude-accounts --rotation-helper --status
launchctl print gui/$(id -u)/ai.claude-accounts.rotation
```

The status contains account labels, quota windows and timestamps, never tokens.
The implementation does not establish Anthropic terms compliance. Review
[Anthropic's authentication and credential-use guidance](https://code.claude.com/docs/en/legal-and-compliance#authentication-and-credential-use)
for your use case; using the official CLI without a proxy does not itself grant
permission for every multi-account or third-party integration.

The credential-reload regression test uses synthetic credentials and a local mock
server. Run `CLAUDE_ROTATION_SMOKE=1 python3 -m unittest test_rotation_runtime -q`
to check your installed CLI without using real account quota. Normal unit tests
skip this installed-CLI test.
