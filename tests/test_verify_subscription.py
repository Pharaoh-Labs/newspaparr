"""Unit tests for _verify_subscription's data-layer retry behaviour."""
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call, patch

import pytest

import renewer
from renewer import RenewalResult, State, _VERIFY_RETRY_DELAYS, _verify_subscription


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow():
    return datetime.now(timezone.utc)


def _future(days=1):
    return (_utcnow() + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stale():
    return (_utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _data_layer_payload(user_type="lgn", ada_end_date=None):
    subs = []
    if ada_end_date is not None:
        subs.append({"bundleType": "ADA", "endDate": ada_end_date})
    return {"user": {"type": user_type, "subInfo": {"subscriptions": subs}}}


def _provisional():
    return RenewalResult(State.RENEWED, "provisional", None, "http://x", 0)


def _make_mock_response(payload):
    resp = MagicMock()
    resp.json.return_value = payload
    return resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestVerifySubscriptionRetry:
    """_verify_subscription should poll data-layer with retries until it gets
    a future endDate, then return RENEWED with that real expiry."""

    @patch("renewer.time.sleep")
    @patch("renewer.extract_cookies", return_value=[{"name": "k", "value": "v",
                                                      "domain": ".nytimes.com"}])
    def test_fresh_date_on_first_attempt_no_sleep(self, _mock_cookies, mock_sleep):
        fresh = _future(days=1)
        with patch("renewer.httpx.Client") as mock_client_cls:
            mock_client_cls.return_value.__enter__.return_value.get.return_value = \
                _make_mock_response(_data_layer_payload(ada_end_date=fresh))

            result = _verify_subscription(1, _provisional(), started=_utcnow())

        assert result.state == State.RENEWED
        assert result.expiration is not None
        assert result.expiration > _utcnow()
        mock_sleep.assert_not_called()

    @patch("renewer.time.sleep")
    @patch("renewer.extract_cookies", return_value=[{"name": "k", "value": "v",
                                                      "domain": ".nytimes.com"}])
    def test_stale_then_fresh_retries_once(self, _mock_cookies, mock_sleep):
        """First call returns stale date; second call returns fresh date."""
        fresh = _future(days=1)
        responses = iter([
            _make_mock_response(_data_layer_payload(ada_end_date=_stale())),
            _make_mock_response(_data_layer_payload(ada_end_date=fresh)),
        ])

        with patch("renewer.httpx.Client") as mock_client_cls:
            mock_client_cls.return_value.__enter__.return_value.get.side_effect = \
                lambda _url: next(responses)

            result = _verify_subscription(1, _provisional(), started=_utcnow())

        assert result.state == State.RENEWED
        assert result.expiration is not None
        assert result.expiration > _utcnow()
        mock_sleep.assert_called_once_with(_VERIFY_RETRY_DELAYS[0])

    @patch("renewer.time.sleep")
    @patch("renewer.extract_cookies", return_value=[{"name": "k", "value": "v",
                                                      "domain": ".nytimes.com"}])
    def test_all_stale_falls_back_to_24h(self, _mock_cookies, mock_sleep):
        """When every attempt returns a stale date we fall back to +24 h."""
        with patch("renewer.httpx.Client") as mock_client_cls:
            mock_client_cls.return_value.__enter__.return_value.get.return_value = \
                _make_mock_response(_data_layer_payload(ada_end_date=_stale()))

            before = _utcnow()
            result = _verify_subscription(1, _provisional(), started=_utcnow())
            after = _utcnow()

        assert result.state == State.RENEWED
        assert result.expiration is not None
        min_expiry = before + timedelta(hours=23, minutes=59)
        max_expiry = after + timedelta(hours=24, minutes=1)
        assert min_expiry < result.expiration < max_expiry, \
            f"Expected ~+24h fallback, got {result.expiration}"

        expected_sleep_calls = [call(d) for d in _VERIFY_RETRY_DELAYS]
        assert mock_sleep.call_args_list == expected_sleep_calls

    @patch("renewer.time.sleep")
    @patch("renewer.extract_cookies", return_value=[{"name": "k", "value": "v",
                                                      "domain": ".nytimes.com"}])
    def test_non_subscriber_user_type_returns_unexpected(self, _mock_cookies, mock_sleep):
        with patch("renewer.httpx.Client") as mock_client_cls:
            mock_client_cls.return_value.__enter__.return_value.get.return_value = \
                _make_mock_response(_data_layer_payload(user_type="anon"))

            result = _verify_subscription(1, _provisional(), started=_utcnow())

        assert result.state == State.UNEXPECTED
        mock_sleep.assert_not_called()

    @patch("renewer.time.sleep")
    @patch("renewer.extract_cookies", return_value=[])
    def test_no_cookies_is_not_reported_as_success(self, _mock_cookies, mock_sleep):
        """Unverifiable must not inherit the provisional RENEWED state — that
        masking is what let months of dead renewals look green."""
        result = _verify_subscription(1, _provisional(), started=_utcnow())
        assert result.state == State.UNVERIFIED
        assert not result.success
        mock_sleep.assert_not_called()

    @patch("renewer.time.sleep")
    @patch("renewer.extract_cookies", return_value=[{"name": "k", "value": "v",
                                                      "domain": ".nytimes.com"}])
    def test_data_layer_error_is_not_reported_as_success(self, _mock_cookies, mock_sleep):
        with patch("renewer.httpx.Client") as mock_client_cls:
            mock_client_cls.return_value.__enter__.return_value.get.side_effect = \
                RuntimeError("boom")

            result = _verify_subscription(1, _provisional(), started=_utcnow())

        assert result.state == State.UNVERIFIED
        assert not result.success


class TestRedemptionCannotSilentlyNoOp:
    """The redemption step must never report success when it did not run."""

    def test_missing_websocket_client_raises(self):
        """Regression: `import websocket` failing used to return None, making
        redemption a no-op that still logged 'NYT pass renewed'."""
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name == "websocket":
                raise ImportError("no module named websocket")
            return real_import(name, *a, **kw)

        with patch.object(builtins, "__import__", side_effect=fake_import):
            with pytest.raises(RuntimeError, match="websocket-client"):
                renewer._navigate_and_await_redemption("ws://x", "https://x?ip_token=y")

    def test_browser_failure_is_not_success(self):
        """A blown-up browser phase reports REDEMPTION_FAILED, not RENEWED."""
        prov = RenewalResult(State.RENEWED, "NYT pass renewed", None,
                             "https://www.nytimes.com/activate-access/ippass?ip_token=x", 0)
        with patch("capture_session._find_chrome_binary",
                   side_effect=FileNotFoundError("no chrome")):
            result = renewer._redeem_via_browser(prov, account_id=1, started=_utcnow())

        assert result.state == State.REDEMPTION_FAILED
        assert not result.success

    def test_deps_status_reports_missing_websocket(self):
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name == "websocket":
                raise ImportError("nope")
            return real_import(name, *a, **kw)

        with patch.object(builtins, "__import__", side_effect=fake_import):
            ok, detail = renewer.redemption_deps_status()

        assert not ok
        assert "websocket-client" in detail
