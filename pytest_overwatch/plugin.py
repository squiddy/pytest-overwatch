import asyncio
import functools
import os
import pathlib
import re
import signal
import subprocess
import sys
import termios
import time
import tty
from contextlib import contextmanager
from multiprocessing import Pipe, Process

from asyncinotify import Inotify, Mask


def pytest_addoption(parser):
    parser.addoption(
        "--watch", action="store_true", dest="watch", default=False,
    )


def pytest_cmdline_main(config):
    if not config.getoption("watch"):
        return

    app = Application(config)

    loop = asyncio.get_event_loop()
    loop.run_until_complete(app.main())

    return True


MODE_START = 1
MODE_FILTER = 2


class Application:
    def __init__(self, config):
        self.config = config
        self.loop = asyncio.get_event_loop()
        self.should_quit = False
        self.mode = None
        self.output = Output()

        self.worker_task = None
        self.worker_process = None

        self.collected_tests = []
        self.selected_tests = []
        self.testname_filter = ""

    def update_selected_tests(self):
        if not self.testname_filter:
            self.selected_tests = self.collected_tests
            return
        else:
            self.selected_tests = [
                name
                for name in self.collected_tests
                if re.search(self.testname_filter, name)
            ]

            self.output.clear_entire_line()
            self.output.print("pattern › ", fg=37, end="")
            self.output.print(self.testname_filter, fg=97, end="")
            self.output.save_cursor()
            self.output.clear_below()
            self.output.print("\n")
            for name in self.selected_tests[:20]:
                self.output.print(f"  {name}")
            self.output.print(f"\n{len(self.selected_tests)} matches")
            self.output.restore_cursor()

    async def collect_tests(self):
        connection, child_connection = Pipe()
        code = f"""
from multiprocessing.connection import Connection
from pytest_overwatch.worker import main
main(Connection({child_connection.fileno()}))"""

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            stdin=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            pass_fds=[child_connection.fileno()],
        )
        connection.send(("collect", self.config.args, vars(self.config.option)))

        # TODO This is still blocking.
        tests = connection.recv()

        await process.wait()
        return tests

    def start_test_run(self):
        if not self.worker_task:
            self.worker_task = self.loop.create_task(self.run_tests())
            self.worker_task.add_done_callback(
                lambda _: setattr(self, "worker_task", None)
            )

    def interrupt_test_run(self):
        self.worker_process.send_signal(signal.SIGINT)

    async def run_tests(self):
        self.output.clear()

        connection, child_connection = Pipe()
        code = f"""
from multiprocessing.connection import Connection
from pytest_overwatch.worker import main
main(Connection({child_connection.fileno()}))"""

        self.worker_process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            stdin=sys.stdin,
            stderr=sys.stderr,
            stdout=sys.stdout,
            pass_fds=[child_connection.fileno()],
        )
        connection.send(
            ("run", self.config.args, vars(self.config.option), self.selected_tests)
        )

        await self.worker_process.wait()
        self.worker_process = None

    def handle_keypress(self, key):
        if self.mode == MODE_START:
            if key == "q":
                if self.worker_task:
                    self.interrupt_test_run()
                else:
                    self.should_quit = True
            elif key == "a":
                self.start_test_run()
            elif key == "p":
                self.start_filename_filter()
            elif key == "w":
                self.show_menu()
        elif self.mode == MODE_FILTER:
            if key == "\x1b":
                self.show_menu()
            elif key == "\x7f":
                self.testname_filter = self.testname_filter[:-1]
                self.update_selected_tests()
            elif ord(key) in [10, 13]:
                if self.selected_tests:
                    self.mode = MODE_START
                    self.start_test_run()
            elif ord(key) < 32:
                pass
            else:
                self.testname_filter = self.testname_filter + key
                self.update_selected_tests()

    def start_filename_filter(self):
        self.mode = MODE_FILTER
        self.output.clear()
        self.output.print("Watch Usage", fg=97, bold=True)
        self.output.print("› Press ", fg=37, end="")
        self.output.print("ESC ", fg=97, end="")
        self.output.print("to exit pattern mode.", fg=37)
        self.output.print("› Press ", fg=37, end="")
        self.output.print("Enter ", fg=97, end="")
        self.output.print("to filter.\n", fg=37)
        self.output.print("pattern › ", fg=37, end="")
        self.output.print(self.testname_filter, fg=97, end="")

    def show_menu(self):
        self.mode = MODE_START
        self.output.clear()
        self.output.print("Watch Usage", fg=97, bold=True)
        self.output.print("› Press ", fg=37, end="")
        self.output.print("a ", fg=97, end="")
        self.output.print("to run all tests.", fg=37)
        self.output.print("› Press ", fg=37, end="")
        self.output.print("p ", fg=97, end="")
        self.output.print("to run tests based on filename.", fg=37)
        self.output.print("› Press ", fg=37, end="")
        self.output.print("q ", fg=97, end="")
        self.output.print("to quit.\n", fg=37)

    async def watch_file_changes(self):
        with Inotify() as inotify:
            for directory, _, _ in os.walk(os.getcwd()):
                # TODO How to best ignore things like .git, .pytest_cache etc?
                path = pathlib.Path(directory)
                if any(p.startswith(".") or p == "__pycache__" for p in path.parts):
                    continue

                inotify.add_watch(
                    directory,
                    Mask.MODIFY
                    | Mask.CREATE
                    | Mask.DELETE
                    | Mask.MOVE
                    | Mask.ONLYDIR
                    | Mask.MASK_CREATE,
                )

            async for event in inotify:
                if event.name.suffix == ".py":
                    self.start_test_run()

    async def main(self):
        self.collected_tests = await self.collect_tests()

        watcher = self.loop.create_task(self.watch_file_changes())

        input_ = Input(self.loop)
        input_.set_callback(self.handle_keypress)

        try:
            with input_.activate():
                self.show_menu()

                while not self.should_quit:
                    await asyncio.sleep(0.1)
        finally:
            watcher.cancel()


class Output:
    """
    Abstract away terminal output handling.
    """

    def __init__(self):
        self._stream = sys.stdout

    def clear(self):
        self._stream.write("\x1b[2J")
        self._stream.write("\x1b[0;0H")
        self._stream.flush()

    def clear_entire_line(self):
        self._stream.write("\r\x1b[2K")
        self._stream.flush()

    def clear_below(self):
        self._stream.write("\x1b[J")
        self._stream.flush()

    def save_cursor(self):
        self._stream.write("\x1b7")
        self._stream.flush()

    def restore_cursor(self):
        self._stream.write("\x1b8")
        self._stream.flush()

    def print(self, text, fg=None, bold=False, end="\n"):
        if not fg and not bold:
            self._stream.write(f"{text}{end}")
            return

        self._stream.write(f"\x1b[{fg}m")
        if bold:
            self._stream.write("\x1b[1m")

        self._stream.write(f"{text}\x1b[0m{end}")
        self._stream.flush()


class Input:
    """
    Abstract away terminal input handling.
    """

    def __init__(self, loop):
        self._buffer = []
        self._callback = None
        self._auto_flush_task = None
        self._loop = loop
        self._stream = sys.stdin

    def _handle_input(self):
        key = sys.stdin.read(1)
        self._buffer.append(key)

        if key == "\x1b":
            # We don't know yet whether the user actually pressed escape, or
            # whether this is the start of a escape sequence.
            # Therefore we wait a bit to collect more input, otherwise we flush
            # the keys.
            if self._auto_flush_task:
                self._auto_flush_task.cancel()
            self._auto_flush_task = self._loop.call_later(0.1, self._flush_keys)
        else:
            self._flush_keys()

    def _flush_keys(self):
        # TODO: Doesn't actually handle escape sequences yet.
        key = self._buffer[:]
        self._buffer.clear()

        if len(key) == 1:
            key = key[0]

        self._callback(key)

    def set_callback(self, callback):
        """
        Register function to be called whenever keypress is detected.
        """
        self._callback = callback

    @contextmanager
    def activate(self):
        """
        Enter cbreak mode which disables (among other things) line buffering
        and allows us to read input as it comes.
        """
        old_terminal_attrs = termios.tcgetattr(self._stream)
        tty.setcbreak(self._stream.fileno())

        self._loop.add_reader(self._stream.fileno(), self._handle_input)

        yield self

        self._loop.remove_reader(self._stream.fileno())
        termios.tcsetattr(self._stream, termios.TCSADRAIN, old_terminal_attrs)
