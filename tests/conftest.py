import pytest

from dataset_builder.config import PathsCfg, load_config


@pytest.fixture
def cfg(tmp_path):
    """The real config, with every path redirected into a temp folder."""
    base = load_config()
    paths = PathsCfg(**{k: tmp_path / k for k in base.paths.model_dump()})
    return base.model_copy(update={"paths": paths})
