from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Union
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

FieldSpec = Union[str, Callable[[Tag], Any]]

URL_ATTRS = frozenset({"href", "src", "data-src", "action", "poster", "data-href"})
_ATTR_RE = re.compile(r"^[\w:.-]+$")


def clean_text(text: str | None) -> str:
    return " ".join((text or "").split())


def parse_field_spec(spec: str) -> tuple[str | None, str | None]:
    spec = spec.strip()
    selector, attr = spec, None
    if "@" in spec:
        head, tail = spec.rsplit("@", 1)
        if _ATTR_RE.match(tail.strip()):
            selector, attr = head.strip(), tail.strip()
    if selector in ("", "."):
        selector = None
    return selector, attr


def extract_field(node: Tag, spec: FieldSpec, base_url: str | None = None) -> Any:
    if callable(spec):
        return spec(node)
    selector, attr = parse_field_spec(spec)
    target = node if selector is None else node.select_one(selector)
    if target is None:
        return None
    if attr is None:
        return clean_text(target.get_text(" "))
    value = target.get(attr)
    if value is None:
        return None
    if isinstance(value, list):
        value = " ".join(value)
    value = value.strip()
    if base_url and attr in URL_ATTRS and value:
        value = urljoin(base_url, value)
    return value


def extract_records(
    html: str,
    item_selector: str,
    fields: Mapping[str, FieldSpec],
    base_url: str | None = None,
    parser: str = "html.parser",
) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, parser)
    return [
        {name: extract_field(item, spec, base_url) for name, spec in fields.items()}
        for item in soup.select(item_selector)
    ]
