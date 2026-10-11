import pytest


@pytest.fixture(autouse=True)
def isolated_pointer(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("KB_BUILDER_HOME", str(tmp_path_factory.mktemp("kbhome")))
    monkeypatch.delenv("KB_ROOT", raising=False)
