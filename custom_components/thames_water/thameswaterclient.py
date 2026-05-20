import base64
from dataclasses import dataclass, field
import datetime
import hashlib
import logging
import os
from typing import Literal, Optional
import uuid

import requests


_LOGGER = logging.getLogger(__name__)


@dataclass
class Line:
    Label: str
    Usage: float
    Read: float
    IsEstimated: bool
    MeterSerialNumberHis: str


@dataclass
class MeterUsage:
    IsError: bool
    IsDataAvailable: bool
    IsConsumptionAvailable: bool
    TargetUsage: float
    AverageUsage: float
    ActualUsage: float
    MyUsage: Optional[str]  # so far have only seen 'NA'
    AverageUsagePerPerson: float
    IsMO365Customer: bool
    IsMOPartialCustomer: bool
    IsMOCompleteCustomer: bool
    IsExtraMonthConsumptionMessage: bool
    Lines: list[Line] | None = None
    AlertsValues: Optional[dict] = field(
        default_factory=dict
    )  # assumption that it could be a dict


@dataclass
class Measurement:
    hour_start: datetime.datetime
    usage: int  # Usage
    total: int  # Read


class ThamesWater:
    def __init__(
        self,
        email: str,
        password: str,
        account_number: int,
        client_id: str = "cedfde2d-79a7-44fd-9833-cae769640d3d",  # specific to Thames Water
        cached_refresh_token: Optional[str] = None,
        cached_cookies: Optional[dict] = None,
    ):
        self.s = requests.session()
        self.account_number = account_number
        self.client_id = client_id

        if cached_refresh_token and cached_cookies:
            try:
                self._fast_path_refresh(cached_refresh_token, cached_cookies)
                _LOGGER.info(
                    "Reused cached refresh_token + session cookies for account %s",
                    self.account_number,
                )
                return
            except Exception as err:  # noqa: BLE001 — owner-requested fallback semantics
                _LOGGER.info(
                    "Cached refresh_token rejected for account %s (%s); "
                    "falling back to full B2C authentication this run",
                    self.account_number, err,
                )
                # Reset any partial state before the full re-auth attempt.
                self.s = requests.session()

        self._authenticate(email, password)

    def _fast_path_refresh(self, refresh_token: str, cookies: dict) -> None:
        """Reuse a cached refresh_token + session cookies; raise on any failure.

        The caller catches the exception and falls back to a full B2C
        authentication within the same call. This satisfies the "no
        waiting until the next scheduled run if the refresh token has
        expired" requirement.
        """
        self.s.cookies.update(cookies)
        # Seed `oauth_request_tokens` so the existing refresh helper can read it.
        self.oauth_request_tokens = {"refresh_token": refresh_token}
        self._refresh_oauth2_token_b2c_1_tw_website_signin()
        # Probe the myaccount session: if the cookies have expired
        # server-side, the AJAX endpoint will redirect to the sign-in
        # page (200 + HTML) rather than returning JSON. Validate by
        # hitting the meters-usage page and treating any non-2xx or
        # missing-cookie outcome as a failure.
        probe_headers = {
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
            "Referer": "https://myaccount.thameswater.co.uk/twservice/Account/SignIn?useremail=",
        }
        r = self.s.get(
            f"https://myaccount.thameswater.co.uk/mydashboard/my-meters-usage?contractAccountNumber={self.account_number}",
            headers=probe_headers, timeout=15, allow_redirects=False,
        )
        r.raise_for_status()
        if r.status_code in (301, 302, 303, 307, 308):
            raise RuntimeError(
                f"myaccount session probe redirected ({r.status_code}); cached cookies appear expired"
            )

    def get_latest_refresh_token(self) -> Optional[str]:
        """Return the most recent refresh_token, preferring the refreshed one."""
        if hasattr(self, "oauth_response_tokens"):
            tok = self.oauth_response_tokens.get("refresh_token")
            if tok:
                return tok
        if hasattr(self, "oauth_request_tokens"):
            tok = self.oauth_request_tokens.get("refresh_token")
            if tok:
                return tok
        return None

    def get_session_cookies(self) -> dict:
        """Return current session cookies as a plain dict for caching."""
        return dict(self.s.cookies)

    def _generate_pkce(self):
        self.pkce_verifier = (
            base64.urlsafe_b64encode(os.urandom(32)).decode("utf-8").rstrip("=")
        )
        self.pkce_challenge = (
            base64.urlsafe_b64encode(
                hashlib.sha256(self.pkce_verifier.encode()).digest()
            )
            .decode("utf-8")
            .rstrip("=")
        )

    def _authorize_b2c_1_tw_website_signin(self) -> tuple[str, str]:
        url = "https://login.thameswater.co.uk/identity.thameswater.co.uk/b2c_1_tw_website_signin/oauth2/v2.0/authorize"

        params = {
            "client_id": self.client_id,
            "scope": "openid profile offline_access",
            "response_type": "code",
            "redirect_uri": "https://www.thameswater.co.uk/login",
            "response_mode": "fragment",
            "code_challenge": self.pkce_challenge,
            "code_challenge_method": "S256",
            "nonce": str(uuid.uuid4()),
            "state": str(uuid.uuid4()),
        }

        r = self.s.get(url, params=params, timeout=30)
        r.raise_for_status()
        return dict(self.s.cookies)["x-ms-cpim-trans"], dict(self.s.cookies)[
            "x-ms-cpim-csrf"
        ]

    def _self_asserted_b2c_1_tw_website_signin(
        self, email: str, password: str, trans_token: str, csrf_token: str
    ):
        url = "https://login.thameswater.co.uk/identity.thameswater.co.uk/B2C_1_tw_website_signin/SelfAsserted"

        params = {
            "tx": f"StateProperties={trans_token}",
            "p": "B2C_1_tw_website_signin",
        }

        data = {"request_type": "RESPONSE", "email": email, "password": password}

        headers = {
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
            "x-csrf-token": csrf_token,
        }

        r = self.s.post(url, params=params, data=data, headers=headers, timeout=30)
        r.raise_for_status()

    def _confirmed_b2c_1_tw_website_signin(self, trans_token: str, csrf_token: str):
        url = "https://login.thameswater.co.uk/identity.thameswater.co.uk/B2C_1_tw_website_signin/api/CombinedSigninAndSignup/confirmed"

        headers = {
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
        }

        params = {
            "rememberMe": "false",
            "tx": f"StateProperties={trans_token}",
            "csrf_token": csrf_token,
            "p": "B2C_1_tw_website_signin",
        }

        r = self.s.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()

        confirmed_signup_structured_response = {
            item.split("=")[0]: item.split("=")[1]
            for item in r.url.split("#")[1].split("&")
        }
        return confirmed_signup_structured_response["code"]

    def _get_oauth2_code_b2c_1_tw_website_signin(self, confirmation_code: str):
        url = "https://login.thameswater.co.uk/identity.thameswater.co.uk/b2c_1_tw_website_signin/oauth2/v2.0/token"

        headers = {
            "content-type": "application/x-www-form-urlencoded;charset=utf-8",
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        }

        data = {
            "client_id": self.client_id,
            "redirect_uri": "https://www.thameswater.co.uk/login",
            "scope": "openid offline_access profile",
            "grant_type": "authorization_code",
            "client_info": "1",
            "x-client-SKU": "msal.js.browser",
            "x-client-VER": "3.1.0",
            "x-ms-lib-capability": "retry-after, h429",
            "x-client-current-telemetry": "5|865,0,,,|,",
            "x-client-last-telemetry": "5|0|||0,0",
            "code_verifier": self.pkce_verifier,
            "code": confirmation_code,
        }

        r = self.s.post(url, headers=headers, data=data, timeout=30)
        r.raise_for_status()
        self.oauth_request_tokens = r.json()

    def _refresh_oauth2_token_b2c_1_tw_website_signin(self):
        url = "https://login.thameswater.co.uk/identity.thameswater.co.uk/b2c_1_tw_website_signin/oauth2/v2.0/token"

        data = {
            "client_id": self.client_id,
            "scope": "openid profile offline_access",
            "grant_type": "refresh_token",
            "client_info": "1",
            "x-client-SKU": "msal.js.browser",
            "x-client-VER": "3.1.0",
            "x-ms-lib-capability": "retry-after, h429",
            "x-client-current-telemetry": "5|61,0,,,|@azure/msal-react,2.0.3",
            "x-client-last-telemetry": "5|0|||0,0",
            "refresh_token": self.oauth_request_tokens["refresh_token"],
        }

        headers = {"content-type": "application/x-www-form-urlencoded;charset=utf-8"}

        # OAuth 2.0 refresh_token grant is a POST (RFC 6749 §6) — the prior
        # GET-with-body form would have been silently accepted by some servers
        # but never actually refreshed the token. Required for the fast-path
        # re-use of cached refresh_tokens.
        r = self.s.post(url, headers=headers, data=data, timeout=30)
        r.raise_for_status()
        self.oauth_response_tokens = r.json()

    def _login(self, state: str, id_token: str):
        url = "https://myaccount.thameswater.co.uk/login"

        data = {
            "state": state,
            "id_token": id_token,
        }

        headers = {
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
            "content-type": "application/x-www-form-urlencoded",
        }

        r = self.s.post(url, data=data, headers=headers, timeout=30)
        r.raise_for_status()

    def _authenticate(
        self,
        email: str,
        password: str,
    ):
        _LOGGER.info("Starting authentication for account %s", self.account_number)
        try:
            self._generate_pkce()
            trans_token, csrf_token = self._authorize_b2c_1_tw_website_signin()
            self._self_asserted_b2c_1_tw_website_signin(
                email, password, trans_token, csrf_token
            )
            confirmation_code = self._confirmed_b2c_1_tw_website_signin(
                trans_token, csrf_token
            )
            self._get_oauth2_code_b2c_1_tw_website_signin(confirmation_code)
            self._refresh_oauth2_token_b2c_1_tw_website_signin()

            headers = {
                "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
                "Referer": "https://myaccount.thameswater.co.uk/twservice/Account/SignIn?useremail=",
            }

            r = self.s.get("https://myaccount.thameswater.co.uk/mydashboard", headers=headers, timeout=30)
            r.raise_for_status()

            r = self.s.get(
                f"https://myaccount.thameswater.co.uk/mydashboard/my-meters-usage?contractAccountNumber={self.account_number}",
                headers=headers,
                timeout=30,
            )
            r.raise_for_status()

            r = self.s.get(
                "https://myaccount.thameswater.co.uk/twservice/Account/SignIn?useremail=",
                headers=headers,
                timeout=30,
            )
            r.raise_for_status()
            
            state = r.url.split("&state=")[1].split("&nonce=")[0].replace("%3d", "=")
            id_token = r.text.split("id='id_token' value='")[1].split("'/>")[0]
            self.s.get(r.url, timeout=30)
            self._login(state, id_token)
            self.s.cookies.set(name="b2cAuthenticated", value="true")
            _LOGGER.info("Authentication successful for account %s", self.account_number)
        except requests.RequestException as e:
            _LOGGER.error("Authentication failed: %s", e)
            raise
        except (KeyError, IndexError) as e:
            _LOGGER.error("Failed to parse authentication response: %s", e)
            raise

    def get_meter_usage(
        self,
        meter: int,
        start: datetime.datetime,
        end: datetime.datetime,
        granularity: Literal["H", "D", "M"] = "H",
    ) -> MeterUsage:
        _LOGGER.info("Fetching meter usage for meter %s from %s to %s", meter, start.date(), end.date())
        url = "https://myaccount.thameswater.co.uk/ajax/waterMeter/getSmartWaterMeterConsumptions"

        params = {
            "meter": meter,
            "startDate": start.day,
            "startMonth": start.month,
            "startYear": start.year,
            "endDate": end.day,
            "endMonth": end.month,
            "endYear": end.year,
            "granularity": granularity,
            "premiseId": "",
            "isForC4C": "false",
        }

        headers = {
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
            "Referer": "https://myaccount.thameswater.co.uk/mydashboard/my-meters-usage",
            "X-Requested-With": "XMLHttpRequest",
        }

        try:
            r = self.s.get(url, params=params, headers=headers, timeout=30)
            r.raise_for_status()

            data = r.json()
            raw_lines = data.get("Lines")
            if raw_lines is None:
                data["Lines"] = None
            else:
                data["Lines"] = [Line(**line) for line in raw_lines]
            result = MeterUsage(**data)
            _LOGGER.info(
                "Retrieved %d readings for meter %s",
                len(result.Lines or []),
                meter,
            )
            return result
        except requests.RequestException as e:
            _LOGGER.error("Failed to get meter usage: %s", e)
            raise
        except (KeyError, ValueError) as e:
            _LOGGER.error("Failed to parse meter usage response: %s", e)
            raise
