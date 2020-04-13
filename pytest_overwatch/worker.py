import pytest
from _pytest.config import Config


def main(connection):
    command, args, option_dict, *rest = connection.recv()
    config = Config.fromdictargs(option_dict, list(args))
    config.args = args

    tests = None
    if command == "collect":
        config.option.collectonly = True
        config.option.verbose = 0
    elif command == "run":
        tests = rest[0] if rest[0] else None

    Session(config, connection).main(tests)


class Session:
    def __init__(self, config, connection):
        self.config = config
        self.connection = connection

        config.option.watch = False
        config.pluginmanager.register(self)

    def main(self, tests):
        self.tests = tests
        self.config.hook.pytest_cmdline_main(config=self.config)

    def pytest_collection_modifyitems(self, session, config, items):
        if self.config.option.collectonly:
            paths = {str(i.fspath) for i in items}
            self.connection.send(list(paths))
            return

        if self.tests:
            items[:] = [item for item in items if str(item.fspath) in self.tests]
