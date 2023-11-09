import asyncio
import os
import pathlib
import signal
import sys
from multiprocessing import Pipe

from textual.app import App

from pytest_overwatch.app import Application

SUPPORTS_INOTIFY = sys.platform.startswith("linux")
if SUPPORTS_INOTIFY:
    from asyncinotify import Inotify, Mask


def pytest_addoption(parser):
    parser.addoption(
        "--watch",
        action="store_true",
        dest="watch",
        default=False,
    )


def pytest_cmdline_main(config):
    if not config.getoption("watch"):
        return

    app = Application(config)

    loop = asyncio.get_event_loop()
    loop.run_until_complete(app.run_async())

    return True


class Pff(App):
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
        self.input.set_callback(self.handle_keypress)
