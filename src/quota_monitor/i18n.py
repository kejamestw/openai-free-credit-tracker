"""Small dependency-free locale loader, fallback, and formatter utilities."""

from __future__ import annotations

import json
import locale as system_locale
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from string import Formatter
from typing import Mapping


DEFAULT_LOCALE_DIRECTORY = Path(__file__).resolve().parents[2] / "locales"
LOCALE_NAME_PATTERN = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*\Z", re.ASCII)
PLACEHOLDER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z", re.ASCII)
RUNTIME_LOCALE_SOURCE_DIRECTORIES = ("src", "web")
RUNTIME_LOCALE_SOURCE_SUFFIXES = frozenset({".py", ".js", ".html"})
DYNAMIC_LOCALE_PREFIX_MARKERS: Mapping[str, tuple[str, ...]] = {
    # These namespaces are selected from bounded server/runtime enums at run time.
    "errors.": ("errors.${", "errors.{"),
    "notification.status_": ("notification.status_${", "notification.status_{"),
    "update.state.": ("update.state.${", "update.state.{"),
}


class LocaleError(ValueError):
    pass


class TranslationFormatError(LocaleError):
    pass


def _flatten_messages(value: object, prefix: str = "") -> dict[str, str]:
    if not isinstance(value, dict):
        raise LocaleError("locale root must be a JSON object")
    flattened: dict[str, str] = {}
    for segment, child in value.items():
        if not isinstance(segment, str) or not segment or "." in segment:
            raise LocaleError("locale key segments must be non-empty and cannot contain dots")
        key = f"{prefix}.{segment}" if prefix else segment
        if isinstance(child, dict):
            nested = _flatten_messages(child, key)
            if not nested:
                raise LocaleError(f"locale group {key!r} cannot be empty")
            flattened.update(nested)
        elif isinstance(child, str) and child.strip():
            flattened[key] = child
        else:
            raise LocaleError(f"locale value {key!r} must be a non-empty string")
    return flattened


def _placeholders(message: str) -> frozenset[str]:
    fields: set[str] = set()
    try:
        parsed = Formatter().parse(message)
        for _literal, field_name, _format_spec, _conversion in parsed:
            if field_name is None:
                continue
            if not PLACEHOLDER_PATTERN.fullmatch(field_name):
                raise LocaleError(f"invalid placeholder {{{field_name}}}")
            fields.add(field_name)
    except ValueError as exc:
        raise LocaleError(f"invalid format string: {exc}") from None
    return frozenset(fields)


def load_locale_file(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LocaleError(f"cannot load locale file {Path(path).name}: {exc}") from None
    return _flatten_messages(payload)


def validate_locale_messages(catalogs: Mapping[str, Mapping[str, str]]) -> tuple[int, int]:
    missing_locales = sorted({"en", "zh-TW"} - set(catalogs))
    if missing_locales:
        raise LocaleError(f"required locales are missing: {', '.join(missing_locales)}")
    baseline_keys = set(catalogs["en"])
    if not baseline_keys:
        raise LocaleError("English fallback locale cannot be empty")
    baseline_placeholders = {
        key: _placeholders(message) for key, message in catalogs["en"].items()
    }
    for locale_name, messages in catalogs.items():
        keys = set(messages)
        missing = sorted(baseline_keys - keys)
        extra = sorted(keys - baseline_keys)
        if missing or extra:
            details = []
            if missing:
                details.append(f"missing: {', '.join(missing)}")
            if extra:
                details.append(f"extra: {', '.join(extra)}")
            raise LocaleError(f"{locale_name} key mismatch ({'; '.join(details)})")
        for key, message in messages.items():
            placeholders = _placeholders(message)
            if placeholders != baseline_placeholders[key]:
                raise LocaleError(
                    f"{locale_name}:{key} placeholder mismatch: "
                    f"expected {sorted(baseline_placeholders[key])}, got {sorted(placeholders)}"
                )
    return len(catalogs), len(baseline_keys)


def load_locale_directory(directory: Path = DEFAULT_LOCALE_DIRECTORY) -> dict[str, dict[str, str]]:
    locale_directory = Path(directory)
    catalogs: dict[str, dict[str, str]] = {}
    if not locale_directory.is_dir():
        raise LocaleError(f"locale directory does not exist: {locale_directory}")
    for path in sorted(locale_directory.glob("*.json")):
        locale_name = path.stem
        if not LOCALE_NAME_PATTERN.fullmatch(locale_name):
            raise LocaleError(f"invalid locale filename: {path.name}")
        catalogs[locale_name] = load_locale_file(path)
    validate_locale_messages(catalogs)
    return catalogs


def find_unused_locale_keys(
    catalogs: Mapping[str, Mapping[str, str]],
    source_root: Path,
) -> tuple[str, ...]:
    """Find catalog keys with no static or declared dynamic runtime consumer.

    Tests, fixtures, and locale files deliberately do not count as consumers.  A
    dynamic namespace is accepted only when its interpolation marker is present
    in production source, preventing arbitrary prefixes from hiding dead keys.
    """

    root = Path(source_root)
    source_documents: list[str] = []
    for directory_name in RUNTIME_LOCALE_SOURCE_DIRECTORIES:
        directory = root / directory_name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.suffix.lower() in RUNTIME_LOCALE_SOURCE_SUFFIXES:
                try:
                    source_documents.append(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError) as exc:
                    raise LocaleError(f"cannot inspect locale consumer {path}: {exc}") from None
    source = "\n".join(source_documents)
    keys = set(catalogs.get("en", {}))
    used: set[str] = set()
    for key in keys:
        boundary = rf"(?<![A-Za-z0-9_.-]){re.escape(key)}(?![A-Za-z0-9_.-])"
        if re.search(boundary, source):
            used.add(key)
    for prefix, markers in DYNAMIC_LOCALE_PREFIX_MARKERS.items():
        if any(marker in source for marker in markers):
            used.update(key for key in keys if key.startswith(prefix))
    return tuple(sorted(keys - used))


def validate_locale_usage(
    catalogs: Mapping[str, Mapping[str, str]],
    source_root: Path,
) -> int:
    unused = find_unused_locale_keys(catalogs, source_root)
    if unused:
        raise LocaleError(f"unused locale keys: {', '.join(unused)}")
    return len(catalogs["en"])


def canonicalize_locale(
    requested: str | None,
    available: tuple[str, ...] | list[str],
    *,
    fallback: str = "en",
) -> str:
    if fallback not in available:
        raise LocaleError(f"fallback locale {fallback!r} is unavailable")
    if not requested:
        return fallback
    normalized_parts = requested.replace("_", "-").split("-")
    normalized = "-".join(
        [normalized_parts[0].lower()]
        + [part.upper() if len(part) == 2 else part.title() for part in normalized_parts[1:]]
    )
    if normalized in available:
        return normalized
    language = normalized_parts[0].lower()
    if language == "zh" and "zh-TW" in available:
        return "zh-TW"
    if language in available:
        return language
    return fallback


def detect_system_locale() -> str | None:
    current, _encoding = system_locale.getlocale()
    return current


def _humanize_key(key: str) -> str:
    leaf = key.rsplit(".", 1)[-1]
    words = re.sub(r"[-_]+", " ", leaf).strip()
    return words[:1].upper() + words[1:] if words else "Message unavailable"


@dataclass(frozen=True)
class LocaleCatalog:
    catalogs: Mapping[str, Mapping[str, str]]
    locale: str
    fallback: str = "en"

    def __post_init__(self) -> None:
        if self.fallback not in self.catalogs or not self.catalogs[self.fallback]:
            raise LocaleError("fallback locale is required and cannot be empty")
        for locale_name, messages in self.catalogs.items():
            if not messages:
                raise LocaleError(f"locale {locale_name!r} cannot be empty")
            for key, message in messages.items():
                if not isinstance(key, str) or not key or not isinstance(message, str) or not message:
                    raise LocaleError("catalog entries must be non-empty strings")
                _placeholders(message)
        selected = canonicalize_locale(
            self.locale,
            list(self.catalogs),
            fallback=self.fallback,
        )
        object.__setattr__(self, "locale", selected)

    @classmethod
    def from_directory(
        cls,
        locale: str | None = None,
        *,
        directory: Path = DEFAULT_LOCALE_DIRECTORY,
        fallback: str = "en",
    ) -> "LocaleCatalog":
        catalogs = load_locale_directory(directory)
        requested = locale if locale is not None else detect_system_locale()
        return cls(catalogs=catalogs, locale=requested or fallback, fallback=fallback)

    @property
    def available_locales(self) -> tuple[str, ...]:
        return tuple(sorted(self.catalogs))

    def with_locale(self, locale: str) -> "LocaleCatalog":
        return LocaleCatalog(self.catalogs, locale, self.fallback)

    def translate(
        self,
        key: str,
        *,
        fallback_text: str | None = None,
        parameters: Mapping[str, object] | None = None,
    ) -> str:
        message = self.catalogs[self.locale].get(key)
        if message is None:
            message = self.catalogs[self.fallback].get(key)
        if message is None:
            message = fallback_text or _humanize_key(key)
        values = {} if parameters is None else dict(parameters)
        required = _placeholders(message)
        missing = sorted(required - values.keys())
        if missing:
            raise TranslationFormatError(
                f"translation {key!r} is missing parameters: {', '.join(missing)}"
            )
        try:
            return message.format_map(values)
        except (KeyError, ValueError, TypeError):
            raise TranslationFormatError(f"translation {key!r} could not be formatted") from None


def _coerce_decimal(value: int | float | Decimal) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise TypeError("value must be numeric")
    return Decimal(str(value))


def format_number(
    value: int | float | Decimal,
    locale: str,
    *,
    decimals: int = 0,
) -> str:
    if not isinstance(decimals, int) or decimals < 0 or decimals > 12:
        raise ValueError("decimals must be between 0 and 12")
    canonicalize_locale(locale, ["en", "zh-TW"])
    return f"{_coerce_decimal(value):,.{decimals}f}"


def format_usd(value: int | float | Decimal, locale: str, *, decimals: int = 2) -> str:
    selected = canonicalize_locale(locale, ["en", "zh-TW"])
    prefix = "US$" if selected == "zh-TW" else "$"
    return prefix + format_number(value, selected, decimals=decimals)


def format_utc(value: datetime, locale: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("value must be a timezone-aware datetime")
    selected = canonicalize_locale(locale, ["en", "zh-TW"])
    utc_value = value.astimezone(timezone.utc)
    if selected == "zh-TW":
        # Python 3.10 on Windows encodes the entire strftime format through
        # the active ANSI locale. Keep directives ASCII-only and compose the
        # localized separators as Unicode in Python instead.
        return (
            f"{utc_value.strftime('%Y')}年{utc_value.strftime('%m')}月"
            f"{utc_value.strftime('%d')}日 {utc_value.strftime('%H:%M')} UTC"
        )
    return utc_value.strftime("%Y-%m-%d %H:%M UTC")


_PSEUDO_TRANSLATION = str.maketrans(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "áƀçđëƒğħïĵķľɱñöþɋřšŧüṽŵẋÿžÁɃÇĐËƑĞĦÏĴĶĽṀÑÖÞɊŘŠŦÜṼŴẊŸŽ",
)


def pseudo_localize(message: str) -> str:
    """Expand visible text while preserving ``str.format`` placeholders."""

    _placeholders(message)
    parts: list[str] = []
    try:
        for literal, field_name, format_spec, conversion in Formatter().parse(message):
            translated = literal.translate(_PSEUDO_TRANSLATION)
            visible_length = sum(not character.isspace() for character in literal)
            if visible_length:
                translated += " " + ("·" * max(1, (visible_length + 2) // 3))
            parts.append(translated)
            if field_name is not None:
                placeholder = "{" + field_name
                if conversion:
                    placeholder += "!" + conversion
                if format_spec:
                    placeholder += ":" + format_spec
                parts.append(placeholder + "}")
    except ValueError as exc:
        raise LocaleError(f"invalid format string: {exc}") from None
    return "［" + "".join(parts) + "］"
