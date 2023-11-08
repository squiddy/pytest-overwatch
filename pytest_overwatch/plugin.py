import asyncio
import functools
import os
import pathlib
import re
import signal
import subprocess
import sys
import termios
import textwrap
import time
import tty
from contextlib import contextmanager
from multiprocessing import Pipe, Process

from rich.console import Console
from rich.style import Style
from rich.theme import Theme

SUPPORTS_INOTIFY = sys.platform.startswith('linux')
if SUPPORTS_INOTIFY:
    from asyncinotify import Inotify, Mask

theme = Theme(
    {
        "header": Style.parse("bright_white bold"),
        "highlight": Style.parse("bright_white"),
        "text": Style.parse("white"),
        "filter_valid": Style.parse("bright_white"),
        "filter_invalid": Style.parse("red"),
    }
)


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
MODE_TESTRUN = 3


class Application:
    def __init__(self, config):
        self.config = config
        self.loop = asyncio.get_event_loop()
        self.should_quit = False
        self.mode = None
        self.output = Output(theme=theme)
        self.input = Input(self.loop)

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
            try:
                r = re.compile(self.testname_filter)
            except Exception:
                pass
            else:
                self.selected_tests = [
                    name
                    for name in self.collected_tests
                    if re.search(self.testname_filter, name)
                ]

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
        return sorted(tests)

    def start_test_run(self):
        if not self.worker_task:
            self.worker_task = self.loop.create_task(self.run_tests())
            self.worker_task.add_done_callback(
                lambda _: setattr(self, "worker_task", None)
            )

    def interrupt_test_run(self):
        self.worker_process.send_signal(signal.SIGINT)

    async def run_tests(self):
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

        while True:
            # TODO this never ends if the worker dies
            if connection.poll(0.1):
                data = connection.recv()
                if data == "sessionfinish":
                    break
                elif data == "enter_pdb":
                    self.input.stop()
                elif data == "leave_pdb":
                    self.input.start()
                else:
                    raise Exception(f"Unexpected data: {data}")

            await asyncio.sleep(0.1)

        await self.worker_process.wait()
        self.worker_process = None

    def handle_keypress(self, key):
        if self.mode == MODE_START:
            if key == "q":
                self.should_quit = True
            elif key == "a":
                self.start_test_run()
                self.render_ui(MODE_TESTRUN)
            elif key == "p":
                self.render_ui(MODE_FILTER)
            elif key == "w":
                self.render_ui(MODE_START)
        elif self.mode == MODE_TESTRUN:
            if key == "q":
                if self.worker_process:
                    self.interrupt_test_run()
                else:
                    self.render_ui(MODE_START)
            elif key == "a":
                self.start_test_run()
                self.render_ui(MODE_TESTRUN)
            elif key == "w":
                if self.testname_filter:
                    self.render_ui(MODE_FILTER)
                else:
                    self.render_ui(MODE_START)
        elif self.mode == MODE_FILTER:
            if key == "\x1b":
                self.testname_filter = ""
                self.render_ui(MODE_START)
            elif key == "\x7f":
                self.testname_filter = self.testname_filter[:-1]
                self.update_selected_tests()
                self.render_ui()
            elif ord(key) in [10, 13]:
                if self.selected_tests:
                    self.start_test_run()
                    self.render_ui(MODE_TESTRUN)
            elif ord(key) < 32:
                pass
            else:
                self.testname_filter = self.testname_filter + key
                self.update_selected_tests()
                self.render_ui()

    def render_ui(self, next_mode=None):
        previous_mode = self.mode
        self.mode = next_mode or previous_mode

        if self.mode == MODE_START:
            # The start menu
            menu = textwrap.dedent(
                """\
                [header]Watch Usage[/]
                › Press [highlight]a[/] to run all tests.
                › Press [highlight]p[/] to run tests based on filename.
                › Press [highlight]q[/] to quit.\n"""
            )
            self.output.clear()
            self.output.print(menu, style="text")
        elif self.mode == MODE_TESTRUN:
            self.output.clear()
        elif self.mode == MODE_FILTER:
            # The menu where you filter tests based on the filename
            if self.mode != previous_mode:
                self.output.clear()
                menu = textwrap.dedent(
                    """\
                    [header]Watch Usage[/]
                    › Press [highlight]ESC[/] to exit pattern mode.
                    › Press [highlight]Enter[/] to filter.
                    """
                )
                self.output.print(menu, style="text")
                self.output.line()
                self.output.print("pattern › ", style="text", end="")

            try:
                r = re.compile(self.testname_filter)
            except Exception:
                filter_valid = False
            else:
                filter_valid = True

            self.output.clear_entire_line()
            self.output.print("pattern › ", style="text", end="")
            self.output.print(
                self.testname_filter,
                style="filter_valid" if filter_valid else "filter_invalid",
                end="",
                markup=False,  # don't interpret filter pattern as markup
            )
            self.output.save_cursor()
            self.output.clear_below()
            self.output.line(2)
            if filter_valid:
                for filepath in self.selected_tests[:20]:
                    filepath = pathlib.Path(filepath)
                    filepath = filepath.relative_to(self.config.rootdir)
                    self.output.print(
                        f"  {filepath.parent}/[highlight]{filepath.name}[/]",
                        style="text",
                    )
                
                self.output.line()
                self.output.print(
                    f"  [highlight]{len(self.selected_tests)}[/] matches",
                    style="text",
                )
            self.output.restore_cursor()

    async def watch_file_changes(self):
        if not SUPPORTS_INOTIFY:
            return
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
        capman = self.config.pluginmanager.getplugin("capturemanager")
        if capman:
            capman.suspend(in_=True)

        self.collected_tests = await self.collect_tests()
        self.selected_tests = self.collected_tests

        watcher = self.loop.create_task(self.watch_file_changes())

        self.input.set_callback(self.handle_keypress)

        try:
            with self.input.activate():
                self.render_ui(MODE_START)
                while not self.should_quit:
                    await asyncio.sleep(0.1)
        finally:
            watcher.cancel()
            if capman:
                capman.resume()


class Output(Console):
    """
    Abstract away terminal output handling.
    """

    def clear(self):
        self.file.write("\x1b[2J")
        self.file.write("\x1b[0;0H")
        self.file.flush()

    def clear_entire_line(self):
        self.file.write("\r\x1b[2K")
        self.file.flush()

    def clear_below(self):
        self.file.write("\x1b[J")
        self.file.flush()

    def save_cursor(self):
        self.file.write("\x1b7")
        self.file.flush()

    def restore_cursor(self):
        self.file.write("\x1b8")
        self.file.flush()


class Input:
    """
    Abstract away terminal input handling.
    """

    def __init__(self, loop):
        self._buffer = []
        self._callback = None
        self._auto_flush_task = None
        self._loop = loop
        self._stream = None
        self._old_terminal_attrs = None

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

    def start(self):
        """
        Enter cbreak mode which disables (among other things) line buffering
        and allows us to read input as it comes.
        """
        self._stream = sys.stdin
        self._old_terminal_attrs = termios.tcgetattr(self._stream)
        tty.setcbreak(self._stream.fileno())

        self._loop.add_reader(self._stream.fileno(), self._handle_input)

    def stop(self):
        self._loop.remove_reader(self._stream.fileno())
        termios.tcsetattr(self._stream, termios.TCSADRAIN, self._old_terminal_attrs)
        self._stream = None

    @contextmanager
    def activate(self):
        self.start()
        yield self
        self.stop()
