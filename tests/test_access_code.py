"""Access-code libraries: nytimes.com/subscription/redeem?gift_code=... links."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

import renewer
from renewer import RenewalResult, State, _verify_subscription, access_code_activation_url

APL_URL = ("https://www.nytimes.com/subscription/redeem"
           "?campaignId=88UXY&gift_code=827fc5d30acb99da")


def _query(url):
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


class TestActivationUrl:
    def test_gift_code_link_becomes_activation_url(self):
        url = access_code_activation_url(APL_URL)
        assert urlparse(url).path == "/activate-access/access-code"
        assert _query(url) == {
            "access_code": "827fc5d30acb99da",
            "source": "access_code_redemption_lp:news",
            "campaignId": "88UXY",
        }

    def test_access_code_param_and_product_path(self):
        url = access_code_activation_url(
            "https://www.nytimes.com/subscription/redeem/all-access?access_code=abc")
        assert _query(url) == {"access_code": "abc",
                               "source": "access_code_redemption_lp:all_access"}

    def test_activation_url_passes_through(self):
        url = ("https://www.nytimes.com/activate-access/access-code"
               "?access_code=abc&campaignId=X")
        assert access_code_activation_url(url) == url

    @pytest.mark.parametrize("url", [
        "https://library.idm.oclc.org/login?url=https://www.nytimes.com/grouppass",
        "https://www.nytimes.com/subscription/redeem?campaignId=88UXY",
        "https://evil.example/subscription/redeem?gift_code=abc",
        "",
    ])
    def test_non_access_code_urls(self, url):
        assert access_code_activation_url(url) is None


@patch("renewer.extract_cookies", return_value=[{"name": "k", "value": "v",
                                                  "domain": ".nytimes.com"}])
def test_renew_skips_library_login_for_access_codes(_mock_cookies):
    sentinel = RenewalResult(State.RENEWED, "ok")
    with patch("renewer.httpx.Client") as mock_client, \
            patch("renewer._redeem_via_browser", return_value=sentinel) as mock_redeem:
        result = renewer.renew(library_url=APL_URL, library_user="", library_pass="",
                               account_id=1)

    assert result is sentinel
    mock_client.assert_not_called()
    provisional = mock_redeem.call_args.args[0]
    assert provisional.final_url == access_code_activation_url(APL_URL)


def _data_layer(subs):
    resp = MagicMock()
    resp.json.return_value = {"user": {"type": "sub", "subInfo": {"subscriptions": subs}}}
    return resp


@patch("renewer.time.sleep")
@patch("renewer.extract_cookies", return_value=[{"name": "k", "value": "v",
                                                  "domain": ".nytimes.com"}])
class TestAccessCodeVerification:
    def _verify(self, subs, **kw):
        prov = RenewalResult(State.RENEWED, "provisional", None, "http://x", 0)
        with patch("renewer.httpx.Client") as mock_client_cls:
            mock_client_cls.return_value.__enter__.return_value.get.return_value = \
                _data_layer(subs)
            return _verify_subscription(1, prov, started=datetime.now(timezone.utc), **kw)

    def test_any_bundle_accepts_non_ada_subscription(self, _cookies, mock_sleep):
        end = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=72)
        result = self._verify([{"bundleType": "NEWS",
                                "endDate": end.strftime("%Y-%m-%dT%H:%M:%SZ")}],
                              any_bundle=True)
        assert result.state == State.RENEWED
        assert result.expiration == end
        mock_sleep.assert_not_called()

    def test_ippass_still_requires_ada(self, _cookies, mock_sleep):
        """Without any_bundle a non-ADA sub is ignored, so the real 72h expiry
        isn't picked up and the +24h stale fallback applies."""
        end = datetime.now(timezone.utc) + timedelta(hours=72)
        result = self._verify([{"bundleType": "NEWS",
                                "endDate": end.strftime("%Y-%m-%dT%H:%M:%SZ")}])
        assert result.expiration < end - timedelta(hours=24)
        assert mock_sleep.called


class TestAccessCodeReply:
    """NYT's redeemAccessCode reply, captured in the browser, decides the result."""

    def _result(self, payload):
        captured = payload if isinstance(payload, str) else json.dumps(payload)
        return renewer._access_code_result(captured, final_url="http://x",
                                           started=datetime.now(timezone.utc))

    def test_success_uses_reply_end_date(self):
        end = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=72)
        result = self._result({"data": {"redeemAccessCode": {
            "success": True, "subscriptionEndDate": end.isoformat()}}})
        assert result.state == State.RENEWED
        assert result.expiration == end

    def test_date_only_end_falls_back_to_duration(self):
        """A date-only end (midnight UTC) would schedule the next renewal
        early; the duration gives the later, real expiry."""
        now = datetime.now(timezone.utc)
        result = self._result({"data": {"redeemAccessCode": {
            "success": True, "subscriptionEndDate": now.date().isoformat(),
            "subscriptionDurationDays": 3}}})
        assert result.expiration >= now + timedelta(days=3)

    @pytest.mark.parametrize("code", ["access_code_expired", "access_code_already_redeemed",
                                      "something_new"])
    def test_errors_fail_the_renewal(self, code):
        result = self._result({"errors": [{"message": code}], "data": None})
        assert result.state == State.REDEMPTION_FAILED
        assert not result.success

    @pytest.mark.parametrize("captured", [
        None, "", "not json",
        json.dumps({"errors": [{"message": "user_already_subscriber"}]}),
        json.dumps({"data": {"redeemAccessCode": {"success": True}}}),
    ])
    def test_falls_back_to_data_layer(self, captured):
        assert renewer._access_code_result(captured, final_url="http://x",
                                           started=datetime.now(timezone.utc)) is None


@pytest.mark.parametrize("value,expected", [
    ("2026-10-23T04:33:00.000Z", datetime(2026, 10, 23, 4, 33, tzinfo=timezone.utc)),
    ("2026-10-22T23:33:00-05:00", datetime(2026, 10, 23, 4, 33, tzinfo=timezone.utc)),
    (1792729980000, datetime(2026, 10, 23, 4, 33, tzinfo=timezone.utc)),
    ("garbage", None),
    (None, None),
])
def test_parse_end_date(value, expected):
    assert renewer._parse_end_date(value) == expected
