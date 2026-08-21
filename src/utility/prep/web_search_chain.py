"""Deterministic metadata search chain for preparation-time grounding.

The chain separates source retrieval from model reasoning. It tries the locked
Bookwalker URL first, then optional search/catalog providers. Provider keys are
read only from environment variables; no key is ever serialized into evidence.
Only URLs on the configured allowlist are returned as metadata evidence.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DEFAULT_PROVIDERS = (
    "bookwalker_direct",
    "brave",
    "ndl",
    "serpapi",
    "tavily",
    "exa",
)
DEFAULT_ALLOWED_HOSTS = ("bookwalker.jp", "ndlsearch.ndl.go.jp")
DEFAULT_BOOKWALKER_URL = "https://bookwalker.jp/search/?qcat=&word="


class MetadataSearchError(RuntimeError):
    """Raised only for an invalid search-chain configuration."""


@dataclass
class SearchHit:
    provider: str
    title: str
    url: str
    snippet: str = ""
    source_url: str = ""
    confidence: str = "medium"


@dataclass
class SearchAttempt:
    provider: str
    status: str
    requested_url: str = ""
    final_url: str = ""
    hits: int = 0
    detail: str = ""


@dataclass
class SearchChainResult:
    query: str
    requested_url: str
    hits: List[SearchHit] = field(default_factory=list)
    attempts: List[SearchAttempt] = field(default_factory=list)

    @property
    def successful_provider(self) -> str:
        return self.hits[0].provider if self.hits else ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "requested_url": self.requested_url,
            "successful_provider": self.successful_provider,
            "hits": [asdict(hit) for hit in self.hits],
            "attempts": [asdict(attempt) for attempt in self.attempts],
        }

    def to_prompt_block(self) -> str:
        """Render bounded, key-free evidence for the fixed system prompt."""
        payload = self.to_dict()
        for hit in payload["hits"]:
            hit["snippet"] = str(hit.get("snippet", ""))[:2000]
        return "<metadata_search_evidence>\n" + json.dumps(
            payload, ensure_ascii=False, indent=2
        ) + "\n</metadata_search_evidence>"


class MetadataSearchChain:
    """Run the configured provider chain once per prep run."""

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        *,
        opener: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.config = dict(config or {})
        self.enabled = bool(self.config.get("enabled", True))
        self.providers = tuple(
            str(value).strip().lower()
            for value in self.config.get("providers", DEFAULT_PROVIDERS)
            if str(value).strip()
        )
        self.allowed_hosts = tuple(
            self._normalize_host(str(value))
            for value in self.config.get("allowed_hosts", DEFAULT_ALLOWED_HOSTS)
            if str(value).strip()
        )
        self.timeout_seconds = float(self.config.get("timeout_seconds", 20))
        self.max_results = max(1, min(20, int(self.config.get("max_results", 5))))
        self.locked_url = str(
            self.config.get("locked_url", DEFAULT_BOOKWALKER_URL)
        ).strip()
        self.keyword_format = str(
            self.config.get(
                "keyword_format",
                "[Japanese title] + [Author in Japanese] + Bookwalker",
            )
        )
        self.opener = opener or urllib.request.urlopen

    @staticmethod
    def _normalize_host(host: str) -> str:
        return host.lower().strip().removeprefix("www.").rstrip(".")

    @staticmethod
    def _redact_url(url: str) -> str:
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        redacted = {
            "api_key", "apikey", "key", "token", "access_token",
            "subscription-token", "x-subscription-token",
        }
        safe_query = [
            (name, "<redacted>" if name.lower() in redacted else value)
            for name, value in query
        ]
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(safe_query), parts.fragment)
        )

    def _host_allowed(self, url: str) -> bool:
        try:
            host = self._normalize_host(urllib.parse.urlsplit(url).hostname or "")
        except ValueError:
            return False
        return bool(host) and any(host == allowed or host.endswith("." + allowed) for allowed in self.allowed_hosts)

    def _request(
        self,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, str, str]:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "LLM-Translator-metadata/1.0 (+local metadata tooling)",
                "Accept": "text/html,application/json,application/xml;q=0.9,*/*;q=0.1",
                **(headers or {}),
            },
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                status = int(getattr(response, "status", 200) or 200)
                final_url = self._redact_url(str(getattr(response, "geturl", lambda: url)() or url))
                raw = response.read()
                charset = response.headers.get_content_charset() if getattr(response, "headers", None) else None
                return status, final_url, raw.decode(charset or "utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            return int(exc.code), self._redact_url(str(exc.geturl() or url)), raw
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MetadataSearchError(str(exc)) from exc

    def _keyword(self, title_jp: str, author_jp: str) -> str:
        title = str(title_jp or "").strip()
        author = str(author_jp or "").strip()
        keyword = self.keyword_format
        replacements = {
            "[Japanese title]": title,
            "[Author in Japanese]": author,
            "{title_jp}": title,
            "{author_jp}": author,
        }
        for old, new in replacements.items():
            keyword = keyword.replace(old, new)
        if keyword == self.keyword_format:
            keyword = " ".join(part for part in (title, author, "Bookwalker") if part)
        return " ".join(keyword.replace("+", " ").split())

    def _bookwalker_url(self, keyword: str) -> str:
        parts = urllib.parse.urlsplit(self.locked_url)
        query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        query["word"] = [keyword]
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query, doseq=True), parts.fragment)
        )

    @staticmethod
    def _dedupe_hits(hits: Iterable[SearchHit], max_results: int) -> List[SearchHit]:
        result: List[SearchHit] = []
        seen: set[str] = set()
        for hit in hits:
            if not hit.url or hit.url in seen:
                continue
            seen.add(hit.url)
            result.append(hit)
            if len(result) >= max_results:
                break
        return result

    def _bookwalker(self, keyword: str, requested_url: str) -> Tuple[List[SearchHit], SearchAttempt]:
        status, final_url, body = self._request(requested_url)
        if not self._host_allowed(final_url):
            return [], SearchAttempt("bookwalker_direct", "rejected_host", requested_url, final_url, detail="redirect left allowlist")
        if status >= 400:
            return [], SearchAttempt("bookwalker_direct", f"http_{status}", requested_url, final_url)
        soup = BeautifulSoup(body, "html.parser")
        page_title = soup.title.get_text(" ", strip=True) if soup.title else "Bookwalker search"
        page_text = soup.get_text(" ", strip=True)[:2000]
        hits: List[SearchHit] = []
        for anchor in soup.find_all("a", href=True):
            href = urllib.parse.urljoin(final_url, str(anchor.get("href")))
            parsed = urllib.parse.urlsplit(href)
            if not self._host_allowed(href) or parsed.path.rstrip("/") in {"", "/search"}:
                continue
            if not any(marker in parsed.path for marker in ("/series/", "/de", "/product/", "/title/")):
                continue
            title = anchor.get_text(" ", strip=True) or page_title
            hits.append(SearchHit("bookwalker_direct", title, href, page_text, final_url, "high"))
        return self._dedupe_hits(hits, self.max_results), SearchAttempt(
            "bookwalker_direct", "ok" if hits else "no_result", requested_url, final_url, len(hits)
        )

    def _json_request(self, url: str, payload: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, str, str]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "User-Agent": "LLM-Translator-metadata/1.0",
                "Accept": "application/json",
                "Content-Type": "application/json",
                **headers,
            },
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                charset = response.headers.get_content_charset() if getattr(response, "headers", None) else None
                return int(getattr(response, "status", 200) or 200), self._redact_url(str(response.geturl())), raw.decode(charset or "utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            return int(exc.code), self._redact_url(str(exc.geturl() or url)), raw
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MetadataSearchError(str(exc)) from exc

    def _provider_key(self, provider: str) -> Optional[str]:
        env_name = str(self.config.get("api_key_env", {}).get(provider, "")).strip()
        if not env_name:
            env_name = {
                "brave": "BRAVE_SEARCH_API_KEY",
                "serpapi": "SERPAPI_API_KEY",
                "tavily": "TAVILY_API_KEY",
                "exa": "EXA_API_KEY",
            }.get(provider, "")
        return os.getenv(env_name) if env_name else None

    def _brave(self, keyword: str) -> Tuple[List[SearchHit], SearchAttempt]:
        key = self._provider_key("brave")
        if not key:
            return [], SearchAttempt("brave", "skipped_missing_key", detail="BRAVE_SEARCH_API_KEY is unset")
        params = urllib.parse.urlencode({
            "q": f"site:bookwalker.jp {keyword}",
            "count": self.max_results,
            "country": "jp",
            "search_lang": "ja",
        })
        endpoint = str(self.config.get("endpoints", {}).get("brave", "https://api.search.brave.com/res/v1/web/search"))
        status, final_url, body = self._request(endpoint + "?" + params, headers={"X-Subscription-Token": key})
        if status >= 400:
            return [], SearchAttempt("brave", f"http_{status}", endpoint)
        data = json.loads(body or "{}")
        hits = [
            SearchHit("brave", str(item.get("title", "")), str(item.get("url", "")), str(item.get("description", "")), str(item.get("url", "")), "medium")
            for item in data.get("web", {}).get("results", [])
            if self._host_allowed(str(item.get("url", "")))
        ]
        hits = self._dedupe_hits(hits, self.max_results)
        return hits, SearchAttempt("brave", "ok" if hits else "no_result", endpoint, final_url, len(hits))

    def _tavily(self, keyword: str) -> Tuple[List[SearchHit], SearchAttempt]:
        key = self._provider_key("tavily")
        if not key:
            return [], SearchAttempt("tavily", "skipped_missing_key", detail="TAVILY_API_KEY is unset")
        endpoint = str(self.config.get("endpoints", {}).get("tavily", "https://api.tavily.com/search"))
        payload = {
            "query": keyword,
            "include_domains": ["bookwalker.jp"],
            "max_results": self.max_results,
            "search_depth": str(self.config.get("tavily_search_depth", "basic")),
            "exact_match": bool(self.config.get("tavily_exact_match", True)),
        }
        status, final_url, body = self._json_request(endpoint, payload, {"Authorization": f"Bearer {key}"})
        if status >= 400:
            return [], SearchAttempt("tavily", f"http_{status}", endpoint)
        data = json.loads(body or "{}")
        hits = [
            SearchHit("tavily", str(item.get("title", "")), str(item.get("url", "")), str(item.get("content", "")), str(item.get("url", "")), "medium")
            for item in data.get("results", [])
            if self._host_allowed(str(item.get("url", "")))
        ]
        hits = self._dedupe_hits(hits, self.max_results)
        return hits, SearchAttempt("tavily", "ok" if hits else "no_result", endpoint, final_url, len(hits))

    def _exa(self, keyword: str) -> Tuple[List[SearchHit], SearchAttempt]:
        key = self._provider_key("exa")
        if not key:
            return [], SearchAttempt("exa", "skipped_missing_key", detail="EXA_API_KEY is unset")
        endpoint = str(self.config.get("endpoints", {}).get("exa", "https://api.exa.ai/search"))
        payload = {
            "query": keyword,
            "includeDomains": ["bookwalker.jp"],
            "numResults": self.max_results,
            "contents": {"highlights": True},
        }
        status, final_url, body = self._json_request(endpoint, payload, {"x-api-key": key})
        if status >= 400:
            return [], SearchAttempt("exa", f"http_{status}", endpoint)
        data = json.loads(body or "{}")
        hits = []
        for item in data.get("results", []):
            url = str(item.get("url", ""))
            highlights = item.get("highlights") or []
            hits.append(SearchHit("exa", str(item.get("title", "")), url, " ".join(map(str, highlights)), url, "medium"))
        hits = self._dedupe_hits((hit for hit in hits if self._host_allowed(hit.url)), self.max_results)
        return hits, SearchAttempt("exa", "ok" if hits else "no_result", endpoint, final_url, len(hits))

    def _serpapi(self, keyword: str) -> Tuple[List[SearchHit], SearchAttempt]:
        key = self._provider_key("serpapi")
        if not key:
            return [], SearchAttempt("serpapi", "skipped_missing_key", detail="SERPAPI_API_KEY is unset")
        endpoint = str(self.config.get("endpoints", {}).get("serpapi", "https://serpapi.com/search.json"))
        params = urllib.parse.urlencode({
            "engine": "google",
            "q": f"site:bookwalker.jp {keyword}",
            "google_domain": "google.co.jp",
            "gl": "jp",
            "hl": "ja",
            "num": self.max_results,
            "api_key": key,
        })
        status, final_url, body = self._request(endpoint + "?" + params)
        if status >= 400:
            return [], SearchAttempt("serpapi", f"http_{status}", endpoint)
        data = json.loads(body or "{}")
        hits = [
            SearchHit("serpapi", str(item.get("title", "")), str(item.get("link", "")), str(item.get("snippet", "")), str(item.get("link", "")), "medium")
            for item in data.get("organic_results", [])
            if self._host_allowed(str(item.get("link", "")))
        ]
        hits = self._dedupe_hits(hits, self.max_results)
        return hits, SearchAttempt("serpapi", "ok" if hits else "no_result", endpoint, final_url, len(hits))

    def _ndl(self, title_jp: str, author_jp: str) -> Tuple[List[SearchHit], SearchAttempt]:
        endpoint = str(self.config.get("endpoints", {}).get("ndl", "https://ndlsearch.ndl.go.jp/api/sru"))
        clauses = []
        if title_jp:
            clauses.append(f'title="{title_jp.replace(chr(34), "")}"')
        if author_jp:
            clauses.append(f'creator="{author_jp.replace(chr(34), "")}"')
        if not clauses:
            return [], SearchAttempt("ndl", "skipped_missing_query")
        params = urllib.parse.urlencode({
            "operation": "searchRetrieve",
            "version": "1.2",
            "maximumRecords": self.max_results,
            "recordSchema": "dcndl",
            "query": " AND ".join(clauses),
        })
        status, final_url, body = self._request(endpoint + "?" + params)
        if status >= 400:
            return [], SearchAttempt("ndl", f"http_{status}", endpoint)
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return [], SearchAttempt("ndl", "invalid_xml", endpoint, final_url)
        hits: List[SearchHit] = []
        for record in root.findall(".//{*}recordData"):
            title = next((str(node.text or "").strip() for node in record.iter() if node.tag.endswith("title") and node.text), "")
            identifier = next((str(node.text or "").strip() for node in record.iter() if node.tag.endswith("identifier") and node.text and str(node.text).startswith("http")), "")
            creator = next((str(node.text or "").strip() for node in record.iter() if node.tag.endswith("creator") and node.text), "")
            if identifier and self._host_allowed(identifier):
                hits.append(SearchHit("ndl", title, identifier, creator, identifier, "medium"))
        hits = self._dedupe_hits(hits, self.max_results)
        return hits, SearchAttempt("ndl", "ok" if hits else "no_result", endpoint, final_url, len(hits))

    def search(self, title_jp: str, author_jp: str = "") -> SearchChainResult:
        keyword = self._keyword(title_jp, author_jp)
        requested_url = self._bookwalker_url(keyword)
        result = SearchChainResult(keyword, requested_url)
        if not self.enabled:
            result.attempts.append(SearchAttempt("chain", "disabled"))
            return result

        handlers: Dict[str, Callable[[], Tuple[List[SearchHit], SearchAttempt]]] = {
            "bookwalker_direct": lambda: self._bookwalker(keyword, requested_url),
            "brave": lambda: self._brave(keyword),
            "ndl": lambda: self._ndl(str(title_jp or "").strip(), str(author_jp or "").strip()),
            "serpapi": lambda: self._serpapi(keyword),
            "tavily": lambda: self._tavily(keyword),
            "exa": lambda: self._exa(keyword),
        }
        for provider in self.providers:
            handler = handlers.get(provider)
            if handler is None:
                result.attempts.append(SearchAttempt(provider, "skipped_unknown_provider"))
                continue
            try:
                hits, attempt = handler()
            except (MetadataSearchError, json.JSONDecodeError) as exc:
                logger.warning("Metadata search provider %s failed: %s", provider, exc)
                hits, attempt = [], SearchAttempt(provider, "error", detail=type(exc).__name__)
            result.attempts.append(attempt)
            result.hits = self._dedupe_hits(hits, self.max_results)
            if result.hits:
                break
        return result
