#!/bin/bash
# Download a versioned standalone executable, verify it, then open onboarding.
set -euo pipefail

ca_workdir=''
cleanup() {
  if [[ -n "$ca_workdir" ]]; then rm -rf "$ca_workdir"; fi
}
trap cleanup EXIT

main() {
  local launch=1 option architecture asset base expected actual
  for option in "$@"; do
    case "$option" in
      --check|--no-launch) launch=0 ;;
      --components=cli|--components=desktop|--components=both) ;;
      --help|-h)
        printf '%s\n' 'Claude Accounts installer (Apple Silicon, macOS 13+)' \
          'Default: install missing Claude dependencies and open onboarding.' \
          '--check: report dependencies without installing anything.' \
          '--no-launch: install without opening the TUI.' \
          '--components=cli|desktop|both: choose apps without the checklist.'
        return 0 ;;
      *) printf 'Unknown installer option: %s\n' "$option" >&2; return 2 ;;
    esac
  done
  if [[ "$(uname -s)" != Darwin ]]; then
    echo 'This release supports macOS only.' >&2; return 1
  fi
  architecture=$(uname -m)
  if [[ "$architecture" != arm64 ]]; then
    echo 'This release requires Apple Silicon. An Intel binary is not available yet.' >&2; return 1
  fi
  if [[ "$(sw_vers -productVersion | cut -d. -f1)" -lt 13 ]]; then
    echo 'macOS 13 or newer is required.' >&2; return 1
  fi
  if [[ "$launch" == 1 ]] && ! (: </dev/tty >/dev/tty) 2>/dev/null; then
    echo 'Run in Terminal to open onboarding, or append: bash -s -- --no-launch' >&2
    return 1
  fi
  # Pin both downloads to one release, avoiding a latest-release race.
  base='https://github.com/deviBuilds/claude-accounts/releases/download/v0.2.0'
  asset="claude-accounts-macos-$architecture"
  ca_workdir=$(mktemp -d "${TMPDIR:-/tmp}/claude-accounts-download.XXXXXX")
  echo 'Downloading Claude Accounts v0.2.0…'
  curl --proto '=https' --tlsv1.2 -fSL --retry 3 --connect-timeout 15 --max-time 300 \
    "$base/$asset" -o "$ca_workdir/$asset"
  curl --proto '=https' --tlsv1.2 -fsSL --retry 3 --connect-timeout 15 --max-time 60 \
    "$base/$asset.sha256" -o "$ca_workdir/checksums"
  expected=$(awk -v name="$asset" '$2 == name { print $1 }' "$ca_workdir/checksums")
  if [[ ! "$expected" =~ ^[a-f0-9]{64}$ ]]; then
    echo 'Missing or invalid release checksum. Nothing was installed.' >&2; return 1
  fi
  actual=$(shasum -a 256 "$ca_workdir/$asset")
  if [[ "${actual%% *}" != "$expected" ]]; then
    echo 'Release checksum mismatch. Nothing was installed; rerun to download again.' >&2; return 1
  fi
  chmod 755 "$ca_workdir/$asset"
  if [[ "$launch" == 1 ]]; then
    # curl | bash occupies stdin; curses and browser login need the real terminal.
    "$ca_workdir/$asset" --install "$@" </dev/tty >/dev/tty 2>&1
  else
    "$ca_workdir/$asset" --install "$@"
  fi
}

# The entire script is parsed before stdin is handed back to the terminal.
main "$@"
