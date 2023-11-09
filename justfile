fix:
    ruff check --fix pytest_overwatch
    ruff format pytest_overwatch

check:
    mypy --strict pytest_overwatch
