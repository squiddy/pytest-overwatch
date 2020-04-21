import pytest
from _pytest.config import Config


def main(connection):
    command, args, option_dict, *rest = connection.recv()

    config = Config.fromdictargs(option_dict, list(args))
    config.args = args

    if command == "collect":
        config.option.collectonly = True
        config.option.verbose = 0
    elif command == "run":
        if rest and rest[0]:
            config.option.file_or_dir = rest[0]
            config.args = rest[0]

    Session(config, connection).main()


class Session:
    def __init__(self, config, connection):
        self.config = config
        self.connection = connection

        config.option.watch = False
        config.pluginmanager.register(self)

    def main(self):
        self.config.hook.pytest_cmdline_main(config=self.config)

    def pytest_collection_modifyitems(self, session, config, items):
        if self.config.option.collectonly:
            paths = {str(i.fspath) for i in items}
            self.connection.send(list(paths))
            return

    def pytest_enter_pdb(self, config, pdb):
        self.connection.send("enter_pdb")

    def pytest_leave_pdb(self, config, pdb):
        self.connection.send("leave_pdb")

    def pytest_sessionfinish(self, session, exitstatus):
        self.connection.send("sessionfinish")
