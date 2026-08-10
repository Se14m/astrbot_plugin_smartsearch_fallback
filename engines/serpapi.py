"""自定义引擎：SerpAPI（真实 Google 搜索结果，需 API Key）。

SerpAPI（https://serpapi.com）返回真实搜索引擎（默认 Google）的结果，
需要 API Key：https://serpapi.com/manage-api-key 注册获取（有免费额度）。

Key 来源（任选其一，按优先级）：
1. 插件 WebUI 配置页的 fallback_api_key 字段（推荐，插件会透传到 plugin_config；兼容旧字段 serpapi_api_key）
2. 环境变量 SERPAPI_API_KEY

接入说明：本文件放在 engines/ 目录下，会被 smartsearch_fallback 自动发现并注册为
引擎 "serpapi"（配置 primary_engine / fallback_engine 时填 serpapi，大小写不敏感）。
修改后需在 AStrBot WebUI 插件管理中重启（热重载）本插件才生效。

详细接入规范见 engines/README.md。
"""

import os

import httpx

ENGINE_NAME = "serpapi"

SERPAPI_ENDPOINT = "https://serpapi.com/search"

# freshness 参数 → SerpAPI tbs 时间范围
_FRESHNESS_TBS = {
    "day": "qdr:d",
    "week": "qdr:w",
    "month": "qdr:m",
    "year": "qdr:y",
}

# content_types 关键词 → SerpAPI tbm 搜索类型
_CONTENT_TYPES_TBM = {
    "news": "nws",
    "doc": "isch",  # 文档类无直接对应，降级为图片搜索不合适，故仅 news 生效
}


def _resolve_api_key(plugin_config: dict | None) -> str:
    """按优先级解析 SerpAPI Key：插件配置 > 环境变量。"""
    cfg = plugin_config or {}
    key = str(cfg.get("fallback_api_key") or cfg.get("serpapi_api_key") or "").strip()
    if not key:
        key = str(os.environ.get("SERPAPI_API_KEY") or "").strip()
    return key


def _build_params(
    query: str,
    max_results: int,
    freshness: str,
    content_types: str,
    api_key: str,
) -> dict:
    """构造 SerpAPI 请求参数。"""
    params: dict = {
        "q": query,
        "api_key": api_key,
        "engine": "google",
        "num": max(1, min(20, int(max_results))),
    }
    fkey = (freshness or "").strip().lower()
    if fkey in _FRESHNESS_TBS:
        params["tbs"] = _FRESHNESS_TBS[fkey]
    ckey = (content_types or "").strip().lower()
    if ckey in _CONTENT_TYPES_TBM:
        params["tbm"] = _CONTENT_TYPES_TBM[ckey]
    return params


def _parse_response(data: dict, max_results: int) -> list[dict]:
    """解析 SerpAPI 响应，统一为 [{title, url, snippet}]。"""
    # 业务层错误（如 key 无效、配额用尽）会以 error 字段返回
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"SerpAPI 返回错误: {data['error']}")

    results: list[dict] = []
    seen: set[str] = set()
    organic = data.get("organic_results") or []
    for item in organic:
        if not isinstance(item, dict):
            continue
        url = str(item.get("link") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        title = str(item.get("title") or "").strip()
        snippet = str(item.get("snippet") or item.get("description") or "").strip()
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= max(1, min(20, int(max_results))):
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
    """调用 SerpAPI 搜索，返回 [{title, url, snippet}]。

    无 key 或调用失败时抛出异常——smartsearch_fallback 编排层会捕获异常并
    自动记录日志、转用兜底引擎，不会中断整体搜索流程。
    """
    api_key = _resolve_api_key(plugin_config)
    if not api_key:
        raise ValueError(
            "SerpAPI 引擎需要 API Key：请在插件 WebUI 配置页填写 fallback_api_key（或旧字段 serpapi_api_key），"
            "或设置环境变量 SERPAPI_API_KEY（https://serpapi.com/manage-api-key 获取）。"
        )

    params = _build_params(query, max_results, freshness, content_types, api_key)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(SERPAPI_ENDPOINT, params=params)
        resp.raise_for_status()
        data = resp.json()

    return _parse_response(data, max_results)
