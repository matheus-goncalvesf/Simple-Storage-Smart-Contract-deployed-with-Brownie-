import pytest

import make_fixtures


@pytest.fixture(scope="session")
def fx(tmp_path_factory):
    return make_fixtures.build_all(tmp_path_factory.mktemp("fixtures"))
