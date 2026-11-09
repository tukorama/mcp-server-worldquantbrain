import base64
import re
from html.parser import HTMLParser

_SECRETS: set[str] = set()

_PATTERNS = [
    (
        re.compile(r"(?i)(authorization['\"]?\s*[:=,]?\s*['\"]?(?:basic|bearer)\s+)[\w+/=.-]+"),
        r"\1***",
    ),
    (re.compile(r"(?i)(['\"]?password['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+"), r"\1***"),
    (re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+"), "***"),
    (re.compile(r"(?i)(\bcookie['\"]?\s*[:=,]?\s*['\"]?)[^'\"\n]+"), r"\1***"),
]
_EMAIL = re.compile(r"\b([\w.+-])[\w.+-]*@([\w-]+\.[\w.-]+)\b")
_DATA_URI = re.compile(r"data:[\w/+.-]+;base64,[A-Za-z0-9+/=\s]+")
_LONG_BASE64 = re.compile(r"\b[A-Za-z0-9+/]{512,}={0,2}")


def register_secret(value: str) -> None:
    """Remember a literal secret so it is masked wherever it shows up in logs or errors."""
    if value:
        _SECRETS.add(value)
        _SECRETS.add(base64.b64encode(value.encode()).decode())


def mask_email(text: str) -> str:
    return _EMAIL.sub(r"\1***@\2", text)


def scrub(text: str) -> str:
    for secret in _SECRETS:
        text = text.replace(secret, "***")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return mask_email(text)


def strip_base64(text: str) -> str:
    return _LONG_BASE64.sub("[binary removed]", _DATA_URI.sub("[image removed]", text))


class _TextExtractor(HTMLParser):
    _BLOCK_TAGS = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._BLOCK_TAGS:
            self.parts.append("\n")
        for name, value in attrs:
            if name in ("href", "src") and value and value.startswith("http"):
                self.links.append(value)

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    parser.feed(strip_base64(markup))
    parser.close()
    text = "".join(parser.parts)
    cleaned = re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()
    # Some announcements are nothing but a link or an embedded video.
    return cleaned or "\n".join(parser.links)
