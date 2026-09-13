"""Web search and page fetching.

Search uses a SearXNG instance when configured, otherwise DuckDuckGo's HTML
endpoint, which needs no API key. Fetching converts HTML to plain text with
the standard library so there is no heavy parsing dependency. Every result
carries its URL and retrieval time so artifacts can cite it.
"""

from __future__ import annotations

import html
import re
import urllib.parse
from html.parser import HTMLParser
from typing import Any

import httpx

from swarm.core.types import now_iso
from swarm.tools.registry import Tool, ToolContext, ToolResult


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "iframe"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article", "pre", "blockquote", "table"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str) -> tuple[str, str]:
    p = _TextExtractor()
    try:
        p.feed(raw)
    except Exception:  # noqa: BLE001 - be forgiving with broken HTML
        pass
    text = html.unescape("".join(p.parts))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip(), " ".join(p.title.split())


_DDG_RESULT = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'(?:<a[^>]+class="result__snippet"[^>]*>(.*?)</a>)?',
    re.S,
)


def parse_ddg(page: str, limit: int) -> list[dict[str, str]]:
    out = []
    for m in _DDG_RESULT.finditer(page):
        href, title, snippet = m.group(1), m.group(2), m.group(3) or ""
        if "uddg=" in href:
            q = urllib.parse.urlparse(href).query
            href = urllib.parse.parse_qs(q).get("uddg", [href])[0]
        title = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
        snippet = html.unescape(re.sub(r"<[^>]+>", "", snippet)).strip()
        if href.startswith("http") and "duckduckgo.com/y.js" not in href:
            out.append({"title": title, "url": href, "snippet": snippet})
        if len(out) >= limit:
            break
    return out


class WebSearchTool(Tool):
    name = "web_search"
    description = "Search the web. Returns titles, URLs and snippets. Follow up with web_fetch to read a page."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "search query"},
            "max_results": {"type": "integer", "description": "1-10, default 8"},
        },
        "required": ["query"],
    }
    category = "network"
    timeout_s = 40.0

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult(ok=False, error="empty query")
        cfg = getattr(ctx.settings, "search", None)
        limit = int(args.get("max_results") or (cfg.max_results if cfg else 8))
        limit = max(1, min(10, limit))
        ua = cfg.user_agent if cfg else "swarm-local-agent/0.1"
        headers = {"User-Agent": ua}
        async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers=headers) as client:
            if cfg and cfg.searxng_url:
                r = await client.get(f"{cfg.searxng_url.rstrip('/')}/search",
                                     params={"q": query, "format": "json"})
                r.raise_for_status()
                results = [
                    {"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")}
                    for x in r.json().get("results", [])[:limit]
                ]
            else:
                r = await client.post("https://html.duckduckgo.com/html/", data={"q": query})
                r.raise_for_status()
                results = parse_ddg(r.text, limit)
        ts = now_iso()
        lines = [f"{i + 1}. {x['title']}\n   {x['url']}\n   {x['snippet']}" for i, x in enumerate(results)]
        return ToolResult(
            ok=True,
            output="\n".join(lines) or "no results",
            data={"results": results, "query": query},
            sources=[{"url": x["url"], "title": x["title"], "retrieved_at": ts} for x in results],
        )


class WebFetchTool(Tool):
    name = "web_fetch"
    description = "Fetch a web page or document by URL and return its readable text."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "max_chars": {"type": "integer", "description": "truncate the text, default 12000"},
        },
        "required": ["url"],
    }
    category = "network"
    timeout_s = 60.0

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = str(args.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            return ToolResult(ok=False, error="url must start with http:// or https://")
        cfg = getattr(ctx.settings, "search", None)
        max_chars = int(args.get("max_chars") or (cfg.fetch_max_chars if cfg else 12000))
        ua = cfg.user_agent if cfg else "swarm-local-agent/0.1"
        async with httpx.AsyncClient(timeout=40, follow_redirects=True, headers={"User-Agent": ua}) as client:
            r = await client.get(url)
            r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            if "html" in ctype or r.text.lstrip().lower().startswith(("<!doctype", "<html")):
                text, title = html_to_text(r.text)
            else:
                text, title = r.text, ""
        ts = now_iso()
        truncated = len(text) > max_chars
        text = text[:max_chars]
        return ToolResult(
            ok=True,
            output=(f"# {title}\n\n" if title else "") + text + ("\n…[truncated]" if truncated else ""),
            data={"url": str(r.url), "title": title, "chars": len(text), "truncated": truncated},
            sources=[{"url": str(r.url), "title": title, "retrieved_at": ts}],
        )
