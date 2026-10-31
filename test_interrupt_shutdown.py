"""Exercise interrupts after the TUI returns, including interpreter shutdown."""
import fcntl
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import unittest


ROOT = Path(__file__).resolve().parent


class ShutdownTests(unittest.TestCase):
    def test_process_boundary_handles_startup_interrupt_and_preserves_failures(self):
        for action, expected, traceback in (
                ('return 0', 0, False),
                ('raise KeyboardInterrupt', 130, False),
                ('raise SystemExit(7)', 7, False),
                ('raise RuntimeError("real failure")', 1, True)):
            with self.subTest(action=action):
                driver = '''
import os, signal, threading, runtime
def shutdown():
    os.kill(os.getpid(), signal.SIGINT)
    print('THREAD_SHUTDOWN_FINISHED', flush=True)
threading._register_atexit(shutdown)
def main():
    ACTION
runtime.run_main(main)
'''.replace('ACTION', action)
                result = subprocess.run([sys.executable, '-c', driver], cwd=ROOT,
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertIn('THREAD_SHUTDOWN_FINISHED', result.stdout)
                self.assertNotIn('KeyboardInterrupt', result.stderr)
                self.assertEqual('Traceback' in result.stderr, traceback)

    def test_late_interrupts_during_curses_and_thread_shutdown(self):
        for entrypoint in ('accounts_tui.py', 'app_entry.py'):
            for quit_key in (b'q', b'\x03'):
                with self.subTest(entrypoint=entrypoint, quit_key=quit_key):
                    self.check_shutdown(entrypoint, quit_key)

    def check_shutdown(self, entrypoint, quit_key):
        # Inject signals into real cleanup hooks; no accounts or network calls.
        driver = '''
import curses, os, runpy, signal, sys, threading
endwin = curses.endwin
def interrupted_endwin():
    for _ in range(3):
        os.kill(os.getpid(), signal.SIGINT)
    endwin()
    for _ in range(3):
        os.kill(os.getpid(), signal.SIGINT)
curses.endwin = interrupted_endwin
def interrupted_shutdown():
    for _ in range(3):
        os.kill(os.getpid(), signal.SIGINT)
    print('THREAD_SHUTDOWN_FINISHED', flush=True)
threading._register_atexit(interrupted_shutdown)
sys.argv = [sys.argv[1], '--demo']
runpy.run_path(sys.argv[0], run_name='__main__')
'''
        master, slave = pty.openpty()
        initial = termios.tcgetattr(slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 110, 0, 0))
        process = subprocess.Popen([sys.executable, '-c', driver, str(ROOT / entrypoint)],
                                   cwd=ROOT, stdin=slave, stdout=slave, stderr=slave,
                                   start_new_session=True,
                                   env={**os.environ, 'TERM': 'xterm-256color'})
        output = bytearray()
        def drain_until(predicate):
            deadline = time.monotonic() + 5
            while not predicate() and time.monotonic() < deadline:
                if select.select([master], [], [], .02)[0]:
                    output.extend(os.read(master, 65536))
        try:
            drain_until(lambda: b'Esc: back' in output)
            self.assertIn(b'Esc: back', output)
            if quit_key == b'q':
                os.write(master, quit_key)
            else:
                process.send_signal(signal.SIGINT)
            drain_until(lambda: process.poll() is not None)
            process.wait(timeout=1)
            while select.select([master], [], [], .02)[0]:
                output.extend(os.read(master, 65536))
            self.assertNotIn(b'Traceback', output, output[-1000:])
            self.assertNotIn(b'KeyboardInterrupt', output)
            self.assertIn(b'THREAD_SHUTDOWN_FINISHED', output)
            self.assertEqual(process.returncode, 0 if quit_key == b'q' else 130)
            self.assertIn(b'\x1b[?1049l', output)
            self.assertNotIn(b'\x1b[3J', output)
            self.assertNotIn(b'\x1b[2J', output[output.rfind(b'\x1b[?1049l'):])
            restored = termios.tcgetattr(slave)
            for attrs in (initial, restored):
                attrs[3] &= ~getattr(termios, 'PENDIN', 0)
            self.assertEqual(restored, initial)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            os.close(master)
            os.close(slave)


if __name__ == '__main__':
    unittest.main()
