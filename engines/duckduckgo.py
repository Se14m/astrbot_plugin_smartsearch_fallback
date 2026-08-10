"""示例自定义引擎：DuckDuckGo Instant Answer（免 API Key）。

这是「任意搜索方式」的接入模板。复制本文件为任意名字（如 bing.py / serpapi.py），
修改 ENGINE_NAME 与 search() 实现即可被 smartsearch_fallback 自动发现。

接入要求（务必满足）：
1. 文件放在插件目录 engines/ 下，文件名任意（不以 _ 开头，以 .py 结尾）。
2. 定义模块级变量 ENGINE_NAME（str），用于 primary_engine / fallback_engine 配置。
3. 定义 async def search(query, max_results, freshness, content_types,
                          plugin_config, provider_settings, timeout) -> list[dict]
   返回 [{"title": str, "url": str, "snippet": str}]。
4. 如需密钥：可直接读环境变量，或从 plugin_config 取自定义键
   （例如在插件配置中约定一个 "serpapi_api_key" 字段，需要你自行在
    _conf_schema.json 中补充对应配置项，插件会原样透传给 search）。

本示例使用 DuckDuckGo Instant Answer API（https://api.duckduckgo.com/），
免费、无需 key，但结果覆盖有限（仅对部分查询返回结构化结果），
生产环境建议替换为更完善的搜索源（如 SerpAPI、Bing、自建搜索等）。
"""

import httpx

ENGINE_NAME = "duckduckgo"

DDG_ENDPOINT = "https://api.duckduckgo.com/"


async def search(
    query: str,
    max_results: int = 5,
    freshness: str = "",
    content_types: str = "",
    plugin_config: dict | None = None,
    provider_settings: dict | None = None,
    timeout: int = 30,
) -> list[dict]:
    """调用 DuckDuckGo Instant Answer API，返回 [{title, url, snippet}]。"""
    params = {
        "q": query,
        "format": "json",
        "no_html": 1,
        "skip_disambig": 1,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(DDG_ENDPOINT, params=params)
        resp.raise_for_status()
        data = resp.json()

    results: list[dict] = []
    seen: set[str] = set()

    def _push(title: str, url: str, snippet: str) -> None:
        if not url or url in seen:
            return
        seen.add(url)
        results.append({"title": str(title), "url": str(url), "snippet": str(snippet)})

    # RelatedTopics（含分组 Topics）
    for topic in data.get("RelatedTopics", []) or []:
        if not isinstance(topic, dict):
            continue
        if "Topics" in topic:  # 分组
            for sub in topic.get("Topics", []) or []:
                _push(sub.get("Text", ""), sub.get("FirstURL", ""), sub.get("Text", ""))
        else:
            _push(topic.get("Text", ""), topic.get("FirstURL", ""), topic.get("Text", ""))

    # 摘要条目（若存在）
    if data.get("AbstractURL"):
        _push(data.get("Heading", "") or query, data["AbstractURL"], data.get("AbstractText", "") or "")

    return results[: max(1, min(20, int(max_results)))]
