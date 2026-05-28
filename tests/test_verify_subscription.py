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
    def test_no_cookies_returns_provisional(self, _mock_cookies, mock_sleep):
        prov = _provisional()
        result = _verify_subscription(1, prov, started=_utcnow())
        assert result is prov
        mock_sleep.assert_not_called()
