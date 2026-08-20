"""Validate local Markdown links and bilingual documentation coverage."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
REQUIRED_BILINGUAL_DOCS = {
    "quick-start.md",
    "config-reference.md",
    "data-reference.md",
    "operations.md",
    "security.md",
    "troubleshooting.md",
    "maintainer-guide.md",
}


def validate_documentation(root: Path = ROOT) -> list[str]:
    findings: list[str] = []
    docs = sorted((root / "docs").rglob("*.md"))
    top_level = [root / name for name in ("README.md", "README.en.md", "SECURITY.md", "CONTRIBUTING.md")]
    for path in [*top_level, *docs]:
        if not path.is_file():
            findings.append(f"missing document: {path.relative_to(root)}")
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="strict")
        except UnicodeError:
            findings.append(f"document is not valid UTF-8: {path.relative_to(root)}")
            continue
        for target_text in MARKDOWN_LINK.findall(source):
            target_text = target_text.strip().split(maxsplit=1)[0].strip("<>")
            parsed = urlsplit(target_text)
            if parsed.scheme or target_text.startswith(("#", "mailto:")):
                continue
            candidate = (path.parent / unquote(parsed.path)).resolve()
            try:
                candidate.relative_to(root.resolve())
            except ValueError:
                findings.append(f"link escapes repository: {path.relative_to(root)} -> {target_text}")
                continue
            if not candidate.exists():
                findings.append(f"broken link: {path.relative_to(root)} -> {target_text}")

    for locale in ("en", "zh-TW"):
        found = {path.name for path in (root / "docs" / locale).glob("*.md")}
        for missing in sorted(REQUIRED_BILINGUAL_DOCS - found):
            findings.append(f"missing {locale} documentation: {missing}")
    return findings


def main() -> None:
    findings = validate_documentation()
    if findings:
        raise SystemExit("documentation validation failed:\n- " + "\n- ".join(findings))
    print("documentation valid: local links resolve and bilingual core coverage is complete")


if __name__ == "__main__":
    main()
