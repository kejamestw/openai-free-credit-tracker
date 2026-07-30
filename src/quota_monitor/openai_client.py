import json
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .version import __version__

BASE_URL = "https://api.openai.com/v1"
ADMIN_KEY_PATTERN = re.compile(r"sk-admin-[A-Za-z0-9_-]{8,}\Z", re.ASCII)


class OpenAIClientError(Exception):
    """A sanitized, user-actionable failure from the upstream API client."""

    def __init__(self, code: str, message: str, http_status: int, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retryable = retryable


HTTP_ERROR_MAP = {
    401: (
        "authentication_failed",
        "OpenAI rejected the Admin API Key. Verify that it is active and belongs to the organization.",
        401,
        False,
    ),
    403: (
        "permission_denied",
        "This Admin API Key is not allowed to read organization usage.",
        403,
        False,
    ),
    429: (
        "rate_limited",
        "OpenAI rate-limited the request. Wait briefly and try again.",
        429,
        True,
    ),
}


def validate_admin_key(admin_key: str) -> None:
    if not isinstance(admin_key, str) or not ADMIN_KEY_PATTERN.fullmatch(admin_key):
        raise OpenAIClientError(
            "invalid_admin_key",
            "Enter a valid Admin API Key beginning with sk-admin-.",
            400,
        )


class OpenAIAdminClient:
    def __init__(self, admin_key: str, timeout: int = 45):
        validate_admin_key(admin_key)
        if not isinstance(timeout, int) or timeout <= 0:
            raise ValueError("timeout must be a positive integer")
        self.admin_key = admin_key
        self.timeout = timeout

    def get(self, path: str, params: dict) -> dict:
        url = f"{BASE_URL}{path}?{urlencode(params, doseq=True)}"
        request = Request(
            url,
            headers={
                "Authorization": f"Bearer {self.admin_key}",
                "Accept": "application/json",
                "User-Agent": f"OpenAI-Free-Credit-Tracker/{__version__}",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except HTTPError as exc:
            mapped = HTTP_ERROR_MAP.get(exc.code)
            if mapped:
                raise OpenAIClientError(*mapped) from None
            if 500 <= exc.code <= 599:
                raise OpenAIClientError(
                    "upstream_unavailable",
                    "OpenAI is temporarily unavailable. Try again later.",
                    502,
                    True,
                ) from None
            raise OpenAIClientError(
                "upstream_request_rejected",
                "OpenAI rejected the usage request. Verify the key and try again.",
                502,
            ) from None
        except (socket.timeout, TimeoutError):
            raise OpenAIClientError(
                "upstream_timeout",
                "The OpenAI request timed out. Check the network and try again.",
                504,
                True,
            ) from None
        except URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise OpenAIClientError(
                    "upstream_timeout",
                    "The OpenAI request timed out. Check the network and try again.",
                    504,
                    True,
                ) from None
            raise OpenAIClientError(
                "offline",
                "OpenAI could not be reached. Check the network, proxy, firewall, and TLS settings.",
                503,
                True,
            ) from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise OpenAIClientError(
                "upstream_response_invalid",
                "OpenAI returned a response that this version cannot read.",
                502,
            ) from None
        if not isinstance(payload, dict):
            raise OpenAIClientError(
                "upstream_response_invalid",
                "OpenAI returned a response that this version cannot read.",
                502,
            )
        return payload
