"""Strict parser for notification and operating-system deep-link input."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit


DEEP_LINK_SCHEME = "openai-free-credit-tracker"
DEEP_LINK_HOST = "open"
_PROFILE_ID_PATTERN = re.compile(r"prof_[0-9a-f]{32}\Z", re.ASCII)
_ALERT_ID_PATTERN = re.compile(r"alert_[A-Za-z0-9_-]{8,80}\Z", re.ASCII)
_UTC_DAY_PATTERN = re.compile(r"20[0-9]{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])\Z", re.ASCII)
_PROJECT_KEY_PATTERN = re.compile(r"(?:all|unattributed|project-[0-9a-f]{24})\Z", re.ASCII)
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_ROUTES: dict[str, dict[str, frozenset[str] | re.Pattern[str]]] = {
    "/dashboard": {
        "profile_id": _PROFILE_ID_PATTERN,
        "view": frozenset({"summary", "projects", "history", "alerts"}),
        "utc_day": _UTC_DAY_PATTERN,
        "project_key": _PROJECT_KEY_PATTERN,
    },
    "/profiles": {
        "profile_id": _PROFILE_ID_PATTERN,
        "action": frozenset({"select", "edit"}),
    },
    "/settings": {
        "section": frozenset({"general", "monitoring", "notifications"}),
    },
    "/alerts": {
        "profile_id": _PROFILE_ID_PATTERN,
        "alert_id": _ALERT_ID_PATTERN,
    },
}


class DeepLinkValidationError(ValueError):
    pass


@dataclass(frozen=True)
class DeepLink:
    route: str
    parameters: tuple[tuple[str, str], ...] = ()

    def get(self, name: str) -> str | None:
        return dict(self.parameters).get(name)

    def as_internal_path(self) -> str:
        if not self.parameters:
            return self.route
        return f"{self.route}?{urlencode(self.parameters, quote_via=quote)}"


def parse_deep_link(value: str) -> DeepLink:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise DeepLinkValidationError("deep link must be a non-empty bounded string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise DeepLinkValidationError("deep link contains a control character")
    secret_markers = ("sk-" + "admin-", "sk-" + "proj-")
    if any(marker in value for marker in secret_markers):
        raise DeepLinkValidationError("deep link must not contain credentials")
    if _INVALID_PERCENT_ESCAPE.search(value):
        raise DeepLinkValidationError("deep link contains invalid percent encoding")

    try:
        parsed = urlsplit(value)
        parsed_port = parsed.port
    except ValueError:
        raise DeepLinkValidationError("deep link URL is invalid") from None
    if parsed.fragment:
        raise DeepLinkValidationError("deep link fragments are not supported")
    if parsed.scheme:
        if (
            parsed.scheme != DEEP_LINK_SCHEME
            or parsed.netloc != DEEP_LINK_HOST
            or parsed.username is not None
            or parsed.password is not None
            or parsed_port is not None
        ):
            raise DeepLinkValidationError("deep link origin is not allowed")
    elif parsed.netloc or not value.startswith("/") or value.startswith("//"):
        raise DeepLinkValidationError("deep link must be an internal route")

    decoded_path = unquote(parsed.path)
    if "\\" in decoded_path or "/../" in f"{decoded_path}/" or "/./" in f"{decoded_path}/":
        raise DeepLinkValidationError("deep link path traversal is not allowed")
    allowed_parameters = _ROUTES.get(decoded_path)
    if allowed_parameters is None:
        raise DeepLinkValidationError("deep link route is not allowed")

    try:
        pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=8,
        )
    except ValueError:
        raise DeepLinkValidationError("deep link query is invalid") from None
    if len({name for name, _value in pairs}) != len(pairs):
        raise DeepLinkValidationError("deep link query contains duplicate parameters")
    normalized: list[tuple[str, str]] = []
    for name, parameter in pairs:
        validator = allowed_parameters.get(name)
        if validator is None:
            raise DeepLinkValidationError(f"deep link parameter {name!r} is not allowed")
        if not parameter or len(parameter) > 200:
            raise DeepLinkValidationError(f"deep link parameter {name!r} is invalid")
        if isinstance(validator, re.Pattern):
            valid = validator.fullmatch(parameter) is not None
        else:
            valid = parameter in validator
        if not valid:
            raise DeepLinkValidationError(f"deep link parameter {name!r} is invalid")
        normalized.append((name, parameter))
    return DeepLink(decoded_path, tuple(sorted(normalized)))


def build_deep_link(route: str, **parameters: str) -> str:
    candidate = route
    if parameters:
        candidate += "?" + urlencode(sorted(parameters.items()), quote_via=quote)
    return parse_deep_link(candidate).as_internal_path()
