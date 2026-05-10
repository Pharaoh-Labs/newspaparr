"""Smoke tests — every public route renders without raising.

These won't catch logic bugs, but they catch "I deleted an import / typoed a
template variable / regressed a route" — exactly the class of mistake that
slipped past me until the sidebar-badge regression was flagged.
"""
import pytest


@pytest.mark.parametrize("path", [
    "/",
    "/accounts",
    "/accounts/add",
    "/libraries",
    "/libraries/add",
    "/logs",
])
def test_route_renders(app_client, path):
    _, client = app_client
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} returned {resp.status_code}"


def test_api_endpoints_return_json(app_client):
    _, client = app_client
    for path in ("/api/accounts", "/api/logs", "/api/status"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert resp.is_json, f"{path} is not JSON: {resp.headers.get('Content-Type')}"


def test_sidebar_badge_renders_on_every_page(app_client):
    """Regression: account_count must be in the context on every page,
    not just the dashboard. (Reported and fixed during the v1.1.0 redesign.)"""
    _, client = app_client
    for path in ("/", "/accounts", "/libraries", "/logs"):
        body = client.get(path).get_data(as_text=True)
        assert "ml-auto" in body and "rounded-full" in body, (
            f"sidebar badge HTML missing on {path}"
        )


def test_renewer_state_enum_complete():
    """The State enum is the contract used by app._execute_renewal — make
    sure every member the app cares about is there."""
    from renewer import State
    expected = {"RENEWED", "NO_SESSION", "SESSION_EXPIRED",
                "LIBRARY_AUTH_FAILED", "NETWORK_ERROR", "UNEXPECTED"}
    actual = {s.name for s in State}
    missing = expected - actual
    assert not missing, f"renewer.State is missing: {missing}"
