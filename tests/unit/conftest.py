import pytest


class _Forbidden:
    """Stands where a real transport or a real connection would be, and refuses to be one."""

    def __init__(self, *args, **kwargs):
        raise AssertionError(
            "a unit test reached the vendor transport or the database: the guard under test "
            "returned instead of refusing, and against a real .env that is a live ingest run"
        )


@pytest.fixture(autouse=True)
def no_vendor_and_no_database(monkeypatch):
    # the guard cases drive main() for real and assert only an exit code, so until now nothing but the guard itself kept this suite off the network and off DATABASE_URL.
    monkeypatch.setattr("ingest.__main__.AlpacaClient", _Forbidden)
    monkeypatch.setattr("ingest.__main__.connect", _Forbidden)
