"""自定义引擎：Bing 网页搜索（免 API Key）。

直接请求 Bing 网页版搜索结果页并解析 HTML，免费、无需 key，
可作为 bocha / anysearch / serpapi 之外的备用引擎。

接入说明：本文件放在 engines/ 目录下，会被 smartsearch_fallback 自动发现并注册为
引擎 "bing"（配置 primary_engine / fallback_engine 时填 bing，大小写不敏感）。
修改后需在 AStrBot WebUI 插件管理中重启（热重载）本插件才生效。

注意：本引擎依赖 Bing 网页版返回结构。若页面结构调整或触发风控导致解析失败，
search() 会抛出异常，编排层会自动记录日志并转用兜底引擎，不影响整体流程。
"""

import html as _html
import re

import httpx

ENGINE_NAME = "bing"

_BING_ENDPOINT = "https://www.bing.com/search"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 结果项：<li class="b_algo"> ... </li>
_ITEM_RE = re.compile(r'<li class="b_algo".*?</li>', re.S)
# 标题链接：<h2><a href="URL">TITLE</a></h2>
_LINK_RE = re.compile(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
# 摘要：<p>...</p>
_SNIPPET_RE = re.compile(r'<p[^>]*>(.*?)</p>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    """去除 HTML 标签并反转义，压缩空白。"""
    text = _TAG_RE.sub(" ", text or "")
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_html(html_text: str, max_results: int) -> list[dict]:
    results: list[dict] = []
    seen: set[str] = set()
    for block in _ITEM_RE.findall(html_text):
        m = _LINK_RE.search(block)
        if not m:
            continue
        url = _html.unescape(m.group(1)).strip()
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


async def search(
    query: str,
    max_results: int = 5,
    freshness: str = "",
    content_types: str = "",
    plugin_config: dict | None = None,
    provider_settings: dict | None = None,
    timeout: int = 30,
) -> list[dict]:
    """调用 Bing 网页搜索，返回 [{title, url, snippet}]。

    失败时抛出异常——smartsearch_fallback 编排层会捕获异常并自动转用兜底引擎。
    """
    n = max(1, min(20, int(max_results)))
    params = {
        "q": query,
        "count": n,
        "setlang": "zh-hans",
    }
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(_BING_ENDPOINT, params=params, headers=_HEADERS)
        resp.raise_for_status()
        html_text = resp.text

    results = _parse_html(html_text, n)
    if not results:
        raise RuntimeError(
            "Bing 网页搜索未解析到结果（可能触发风控或页面结构变化）。"
            "可改用 bocha / serpapi 等引擎。"
        )
    return results
