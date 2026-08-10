"""自定义引擎：Google 搜索（优先官方 Custom Search JSON API，无 key 时降级网页解析）。

两种模式（按优先级）：
1. 官方 API：配置了 google_api_key + google_cx（WebUI 插件配置页或环境变量
   GOOGLE_API_KEY / GOOGLE_CX）时使用 Google Custom Search JSON API，结果稳定。
   注册入口：https://programmablesearchengine.google.com/ 创建搜索项目获取 cx，
   https://console.cloud.google.com/ 获取 API Key（Custom Search API）。
2. 免 key 降级：未配置 key 时直接请求 google.com 网页版并解析 HTML。
   Google 网页版反爬较严，可能被 429 / 验证页拦截；解析失败会抛出异常，
   编排层自动转用兜底引擎，不影响整体流程。

接入说明：本文件放在 engines/ 目录下，会被 smartsearch_fallback 自动发现并注册为
引擎 "google"（配置 primary_engine / fallback_engine 时填 google，大小写不敏感）。
修改后需在 AStrBot WebUI 插件管理中重启（热重载）本插件才生效。
"""

import html as _html
import os
import re
from urllib.parse import parse_qs, unquote, urlparse

import httpx

ENGINE_NAME = "google"

_GOOGLE_API_ENDPOINT = "https://www.googleapis.com/customsearch/v1"
_GOOGLE_WEB_ENDPOINT = "https://www.google.com/search"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 结果块：<div class="g"> ... （到下个同级块或结尾）
_G_BLOCK_RE = re.compile(r'<div class="g"[^>]*>(.*?)(?=<div class="g"[^>]*>|$)', re.S)
# 块内标题链接：<a href="/url?q=..."><h3>...</h3></a> 或直接 <a href="http...">
_H3_LINK_RE = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>\s*<h3[^>]*>(.*?)</h3>', re.S)
# 块内摘要：<div class="VwiC3b">...</div>
_SNIPPET_RE = re.compile(r'<div class="VwiC3b"[^>]*>(.*?)</div>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    text = _TAG_RE.sub(" ", text or "")
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_url(href: str) -> str:
    """解析 Google 结果链接：/url?q=REAL&sa=... 或直接 http(s) 链接。"""
    href = _html.unescape(href).strip()
    if not href.startswith("/url"):
        return href if href.startswith("http") else ""
    q = parse_qs(urlparse(href).query).get("q", [""])[0]
    return unquote(q).strip()


def _parse_html(html_text: str, max_results: int) -> list[dict]:
    results: list[dict] = []
    seen: set[str] = set()
    for block in _G_BLOCK_RE.findall(html_text):
        m = _H3_LINK_RE.search(block)
        if not m:
            continue
        url = _extract_url(m.group(1))
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        title = _clean(m.group(2))
        p = _SNIPPET_RE.search(block)
        snippet = _clean(p.group(1)) if p else ""
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= max_results:
            break
    return results


def _resolve_api(plugin_config: dict | None) -> tuple[str, str]:
    cfg = plugin_config or {}
    key = str(cfg.get("google_api_key") or "").strip() or os.environ.get("GOOGLE_API_KEY", "").strip()
    cx = str(cfg.get("google_cx") or "").strip() or os.environ.get("GOOGLE_CX", "").strip()
    return key, cx


async def _search_api(
    query: str, max_results: int, api_key: str, cx: str, timeout: int
) -> list[dict]:
    params = {
        "key": api_key,
        "cx": cx,
        "q": query,
        "num": max(1, min(10, int(max_results))),  # Custom Search API 单次上限 10
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(_GOOGLE_API_ENDPOINT, params=params)
        resp.raise_for_status()
        data = resp.json()
    if data.get("error"):
        raise RuntimeError(f"Google Custom Search API 返回错误: {data['error']}")
    results: list[dict] = []
    for item in data.get("items") or []:
        url = str(item.get("link") or "").strip()
        if not url:
            continue
        results.append({
            "title": str(item.get("title") or "").strip(),
            "url": url,
            "snippet": str(item.get("snippet") or "").strip(),
        })
    return results


async def _search_html(query: str, max_results: int, timeout: int) -> list[dict]:
    n = max(1, min(20, int(max_results)))
    params = {"q": query, "num": n, "hl": "zh-CN"}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(_GOOGLE_WEB_ENDPOINT, params=params, headers=_HEADERS)
        resp.raise_for_status()
        html_text = resp.text
    if "unusual traffic" in html_text or "enablejs=1" in html_text:
        raise RuntimeError("Google 网页搜索触发风控/验证页，请配置 google_api_key + google_cx 使用官方 API")
    results = _parse_html(html_text, n)
    if not results:
        raise RuntimeError("Google 网页搜索未解析到结果，请配置 google_api_key + google_cx 使用官方 API")
    return results


async def search(
    query: str,
    max_results: int = 5,
    freshness: str = "",
    content_types: str = "",
    plugin_config: dict | None = None,
    provider_settings: dict | None = None,
    timeout: int = 30,
) -> list[dict]:
    """调用 Google 搜索，返回 [{title, url, snippet}]。

    优先官方 Custom Search API（需 google_api_key + google_cx）；
    未配置时降级网页版解析。失败时抛出异常，由编排层自动转用兜底引擎。
    """
    api_key, cx = _resolve_api(plugin_config)
    if api_key and cx:
        return await _search_api(query, max_results, api_key, cx, timeout)
    return await _search_html(query, max_results, timeout)
