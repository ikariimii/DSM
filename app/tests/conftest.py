import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from backend.config import Settings  # noqa: E402
from backend.main import create_app  # noqa: E402

ADMIN = "admin@demo.local"
ENG = "engineer.crd@demo.local"
VIEWER = "viewer.bfk@demo.local"
DSO = "dso.phr@demo.local"
NOBODY = "nobody@demo.local"


class Client:
    def __init__(self, tc):
        self.tc = tc

    def _h(self, user):
        return {"x-forwarded-user": f"uid-{user.lower()}", "x-forwarded-email": user} if user else {}

    def get(self, url, user=ADMIN, **kw):
        return self.tc.get(url, headers=self._h(user), **kw)

    def post(self, url, user=ADMIN, **kw):
        return self.tc.post(url, headers=self._h(user), **kw)

    def put(self, url, user=ADMIN, **kw):
        return self.tc.put(url, headers=self._h(user), **kw)

    def delete(self, url, user=ADMIN, **kw):
        return self.tc.delete(url, headers=self._h(user), **kw)


@pytest.fixture(scope="session")
def demo_settings(tmp_path_factory):
    return Settings(demo_mode=True, static_dir=str(tmp_path_factory.mktemp("nodist")))


@pytest.fixture()
def app(demo_settings):
    return create_app(demo_settings)


@pytest.fixture()
def c(app):
    return Client(TestClient(app))


@pytest.fixture(scope="session")
def shared_app(demo_settings):
    return create_app(demo_settings)


@pytest.fixture()
def rc(shared_app):
    """Read-only client on a session-scoped app (fast)."""
    return Client(TestClient(shared_app))
