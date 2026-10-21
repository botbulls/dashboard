from __future__ import annotations

from unittest import mock

import requests

from futuresboard.app import init_app
from futuresboard.config import Config


def test_render_does_not_call_external_ip_services(tmp_path):
    """Antes cada render consultaba api.ipify.org (inject_server_ip); ya no se usa."""
    cfg = Config(
        CONFIG_DIR=tmp_path,
        DATABASE=tmp_path / "futures.db",
        API_KEY="x",
        API_SECRET="x",
        DISABLE_AUTO_SCRAPE=True,
    )
    app = init_app(cfg)
    boom = mock.Mock(side_effect=AssertionError("llamada HTTP saliente inesperada"))
    with mock.patch.object(requests, "get", boom), mock.patch.object(
        requests.Session, "request", boom
    ):
        resp = app.test_client().get("/login")
    assert resp.status_code == 200
    assert not boom.called
    assert "server_ip" not in app.jinja_env.globals
