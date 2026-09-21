import pytest

from wayback_seo.config import DEFAULTS, load


def test_creates_the_file_with_the_defaults(tmp_path):
    path = tmp_path / "config.toml"
    assert load(path) == DEFAULTS
    assert "cache_limit_mb = 100 " in path.read_text()


def test_file_values_override_defaults(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("cache_limit_mb = 200\n")
    assert load(path) == {**DEFAULTS, "cache_limit_mb": 200}


@pytest.mark.parametrize("text", ["cache_limit = 5\n", "port = 'x'\n", "port = 0\n", "port =\n"])
def test_bad_files_are_reported(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    with pytest.raises(ValueError):
        load(path)
