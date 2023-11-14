import asyncio
import re
import sys
from dataclasses import dataclass
from enum import Enum
from multiprocessing import Pipe
from typing import TYPE_CHECKING

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, LoadingIndicator, Static

if TYPE_CHECKING:
    from typing import Any, Generator

    from _pytest.config import Config
    from textual.events import Event


class FilterType(Enum):
    BY_PATH = "path"
    BY_NAME = "name"


@dataclass
class TestsCollected(Message):
    tests: Any


class Start(Screen[None]):
    TITLE = "Start"
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("p", "push_screen('filter_path')", "Filter tests by path"),
        ("t", "push_screen('filter_name')", "Filter tests by name"),
        ("w", "push_screen('run')", "Run tests"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(Static("Collecting tests..."), LoadingIndicator())
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self.collect_tests(), thread=True)

    def on_tests_collected(self, event: Event) -> None:
        self.app.tests = event.tests  # type: ignore
        tests = self.app.tests  # type: ignore

        w = self.query_one(Vertical)
        w.remove_children()
        w.mount(Static(f"{len(tests)} tests"))

    async def collect_tests(self) -> None:
        config = self.app.config  # type: ignore

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
        connection.send(("collect", config.args, vars(config.option)))

        tests = connection.recv()
        await process.wait()
        self.post_message(TestsCollected(tests))


class FilterTests(Screen[None]):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
    ]
    CSS = """
    #query {
        margin-top: 1;
    }
    #stats {
        margin: 1;
    }
    #result {
        margin: 1;
    }
    """

    def __init__(self, filter_type: FilterType, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.filter_type = filter_type

    @on(Input.Changed)
    def query_change(self, message: Any) -> None:
        self.update_results(list(self.filter_tests(message.value)))

    def update_results(self, tests: Any) -> None:
        all_tests = self.app.tests  # type: ignore

        text = Text()
        for path, nodeid in tests:
            if self.filter_type == FilterType.BY_PATH:
                text.append(f"{path}\n")
            elif self.filter_type == FilterType.BY_NAME:
                text.append(f"{nodeid}\n")
            else:
                raise Exception("unexpected filter type")

        self.query_one("#result", Static).update(text)
        assert isinstance(self.app, Application)
        self.query_one("#stats", Static).update(
            f"Matching {len(tests)} / {len(all_tests)}"
        )

    def filter_tests(self, query: str) -> Generator[Any, None, None]:
        tests = self.app.tests  # type: ignore

        matcher = re.compile(f".*{query}.*")
        for path, nodeid in tests:
            if not matcher.match(nodeid):
                continue
            yield (path, nodeid)

    def on_mount(self) -> None:
        tests = self.app.tests  # type: ignore
        self.update_results(tests)

        if self.filter_type == FilterType.BY_PATH:
            self.title = "Filter tests by path"
        elif self.filter_type == FilterType.BY_NAME:
            self.title = "Filter tests by name"
        else:
            raise Exception("unexpected filter type")

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(id="query")
        yield Static("sdf", id="stats")
        yield VerticalScroll(Static("", id="result"))
        yield Footer()


class RunTests(Screen[None]):
    TITLE = "Run tests"
    BINDINGS = [
        ("escape", "pop_screen", "Back"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Footer()


class Application(App[None]):
    TITLE = "pytest-watch"
    SCREENS = {
        "start": Start(),
        "filter_path": FilterTests(FilterType.BY_PATH),
        "filter_name": FilterTests(FilterType.BY_NAME),
        "run": RunTests(),
    }

    BINDINGS = [
        ("q", "quit", "Quit"),
    ]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.tests = None

    def on_mount(self) -> None:
        self.push_screen("start")

    def compose(self) -> ComposeResult:
        yield Header()
        yield Footer()
