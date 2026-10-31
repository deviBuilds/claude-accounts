"""Choose Claude or Codex without mixing their logins, history, or daemons."""
import argparse
import curses
import signal
import sys
import runtime


def choose():
    cancelled = False
    def interrupt(*_):
        nonlocal cancelled
        cancelled = True
    previous_interrupt = signal.signal(signal.SIGINT, interrupt)
    def screen(stdscr):
        cursor = 0
        stdscr.keypad(True)
        stdscr.timeout(100)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        colors = curses.has_colors()
        if colors:
            curses.start_color(); curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_WHITE, curses.COLOR_BLUE)
        def put(y, text, selected=False, heading=False):
            h, w = stdscr.getmaxyx()
            if y >= h-1:
                return
            attr = curses.color_pair(2 if selected else 1 if heading else 0) if colors else curses.A_REVERSE if selected else 0
            try:
                stdscr.addnstr(y, 3, text, max(0, w-6), attr | (curses.A_BOLD if heading or selected else 0))
            except curses.error:
                pass
        while not cancelled:
            stdscr.erase()
            put(1, 'AI ACCOUNTS', heading=True)
            put(3, 'Choose the provider to manage', heading=True)
            put(6, ('› ' if cursor == 0 else '  ')+'Claude  /  CLI and Desktop', cursor == 0)
            put(7, '  Saved accounts, shared local history, and rotation')
            put(10, ('› ' if cursor == 1 else '  ')+'Codex   /  CLI and Desktop', cursor == 1)
            put(11, '  One login for both apps; shared threads and rotation')
            put(14, ('› ' if cursor == 2 else '  ')+'Quit', cursor == 2)
            put(17, '↑/↓ move   Enter: select   Esc: quit', heading=True)
            stdscr.refresh()
            key = stdscr.getch()
            if key in (27, ord('q')):
                return None
            if key in (curses.KEY_UP, ord('k')):
                cursor = (cursor-1) % 3
            elif key in (curses.KEY_DOWN, ord('j'), 9):
                cursor = (cursor+1) % 3
            elif key in (10, 13, curses.KEY_ENTER):
                return ('claude', 'codex', None)[cursor]
        return None
    try:
        result = curses.wrapper(screen)
        if cancelled:
            raise KeyboardInterrupt
        return result
    finally:
        signal.signal(signal.SIGINT, previous_interrupt)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--provider', choices=('claude', 'codex'))
    parsed, remaining = parser.parse_known_args(args)
    if any(v in remaining for v in ('--cli', '--help', '-h')) and parsed.provider is None:
        import accounts_tui
        return accounts_tui.main(remaining)
    provider = parsed.provider
    while True:
        if provider is None:
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                print('Open the account manager in an interactive terminal.', file=sys.stderr)
                return 2
            try:
                provider = choose()
            except KeyboardInterrupt:
                return 130
        if provider is None:
            return 0
        if provider == 'codex':
            import codex_tui
            result = codex_tui.main(remaining)
        else:
            import accounts_tui
            result = accounts_tui.main(remaining)
        if result != 20:
            return result
        provider = None
        remaining = [x for x in remaining if x != '--onboard']


if __name__ == '__main__':
    runtime.run_main(main)
