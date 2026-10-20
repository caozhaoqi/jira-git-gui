"""Verify that the backend serves its HTML entry point and every referenced asset."""
from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from urllib.request import urlopen


class AssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.assets: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name in {"src", "href"} and value and value.startswith("/web/assets/"):
                self.assets.add(value)


def main() -> int:
    base_url = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787").rstrip("/")
    with urlopen(f"{base_url}/", timeout=5) as response:
        html = response.read().decode("utf-8")

    parser = AssetParser()
    parser.feed(html)
    if not any(re.search(r"\.js(?:\?|$)", asset) for asset in parser.assets):
        raise RuntimeError("Root page does not reference a JavaScript bundle under /web/assets/")
    if not any(re.search(r"\.css(?:\?|$)", asset) for asset in parser.assets):
        raise RuntimeError("Root page does not reference a CSS bundle under /web/assets/")

    for asset in sorted(parser.assets):
        with urlopen(f"{base_url}{asset}", timeout=5) as response:
            if not response.read(1):
                raise RuntimeError(f"Empty frontend asset: {asset}")

    print(f"Frontend smoke test passed: root page and {len(parser.assets)} assets served.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
