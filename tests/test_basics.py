from pytest_overwatch import __version__


def test_version():
    assert __version__ == "0.1.0"


def test_foobar():
    assert 1 == 1


def test_barbaz():
    import pdb

    pdb.set_trace()
