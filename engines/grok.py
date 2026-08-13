"""自定义引擎：Grok（xAI 官方 Web Search，服务端工具，需 API Key）。

xAI（https://x.ai）的 Grok 提供内置 Web Search 服务端工具：在 Chat Completions
请求中声明 tools=[{"type": "web_search"}] 后，搜索由 xAI 服务器自动执行，
无需自己实现抓取。本引擎把 Grok 的搜索结果（title/url/snippet）提取为
smartsearch_fallback 统一的结果列表格式。

API Key：https://console.x.ai 注册获取（需为账户充值，无免费额度）。

Key 来源（任选其一，按优先级）：
1. 插件 WebUI 配置页的 grok_api_key 字段（方案A拆分的专用槽位，推荐）
2. 环境变量 XAI_API_KEY

接入说明：本文件放在 engines/ 目录下，会被 smartsearch_fallback 自动发现并注册为
引擎 "grok"（配置 primary_engine / fallback_engine 时填 grok，大小写不敏感）。
修改后需在 AStrBot WebUI 插件管理中重启（热重载）本插件才生效。

详细接入规范见 engines/README.md。
"""

import os

import httpx

ENGINE_NAME = "grok"

# 默认端点与模型（可在插件 WebUI 配置 grok_base_url / grok_model 覆盖）
DEFAULT_BASE_URL = "https://api.x.ai/v1"
DEFAULT_MODEL = "grok-4-fast"

# freshness 参数 → 附加给模型的时效约束（xAI web_search 无独立 freshness 参数，用提示词约束）
_FRESHNESS_PROMPT = {
    "day": "请只返回最近 24 小时内发布或更新的信息。",
    "week": "请优先返回最近一周内发布或更新的信息。",
    "month": "请优先返回最近一个月内发布或更新的信息。",
    "year": "请优先返回最近一年内发布或更新的信息。",
}

# content_types 关键词 → 附加给模型的类型约束
_CONTENT_TYPES_PROMPT = {
    "news": "请优先返回新闻媒体（news）来源的结果。",
    "doc": "请优先返回文档/官方资料（documentation/official）来源的结果。",
}


def _resolve_api_key(plugin_config: dict | None) -> str:
    """按优先级解析 xAI Key：grok_api_key > 环境变量 XAI_API_KEY。"""
    cfg = plugin_config or {}
    key = str(cfg.get("grok_api_key") or "").strip()
    if not key:
        key = str(os.environ.get("XAI_API_KEY") or "").strip()
    return key


def _resolve_settings(plugin_config: dict | None) -> tuple[str, str]:
    """解析端点与模型：插件配置 > 环境变量 > 默认值。"""
    cfg = plugin_config or {}
    base_url = str(cfg.get("grok_base_url") or os.environ.get("XAI_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
    model = str(cfg.get("grok_model") or os.environ.get("XAI_MODEL") or DEFAULT_MODEL).strip()
    return base_url or DEFAULT_BASE_URL, model or DEFAULT_MODEL


def _build_prompt(query: str, freshness: str, content_types: str, max_results: int) -> str:
    """构造搜索提示词：把 freshness/content_types/max_results 约束进自然语言指令。"""
    parts: list[str] = [
        "你是一个搜索引擎。请针对用户查询执行网络搜索，并从搜索结果中提取信息。",
        f"请确保提取并返回至少 {max(1, min(20, int(max_results)))} 条可访问的网页来源（尽量多给，宁多勿少）。",
    ]
    fkey = (freshness or "").strip().lower()
    if fkey in _FRESHNESS_PROMPT:
        parts.append(_FRESHNESS_PROMPT[fkey])
    ckey = (content_types or "").strip().lower()
    if ckey in _CONTENT_TYPES_PROMPT:
        parts.append(_CONTENT_TYPES_PROMPT[ckey])
    parts.append("不要输出总结性回答，重点是提供来源列表。")
    return "\n".join(parts)


def _extract_search_results(data: dict, max_results: int) -> list[dict]:
    """从 xAI Chat Completions 响应中提取搜索结果，统一为 [{title, url, snippet}]。

    兼容多种响应形态（xAI 版本差异较大，防御性解析）：
    a. choices[0].message.search_results（xAI 扩展字段，推荐形态）
    b. 顶层 data.search_results（部分版本把结果放在响应根级）
    c. choices[0].message.tool_calls 内嵌 web_search 的 server-side 结果
    d. choices[0].message.citations（纯 URL 列表，降级为无摘要条目）
    全部缺失时抛出 RuntimeError，由编排层转兜底引擎。
    """
    results: list[dict] = []
    seen: set[str] = set()

    def _add(item: dict) -> None:
        if not isinstance(item, dict):
            return
        url = str(item.get("url") or item.get("link") or "").strip()
        if not url:
            return
        if url in seen:
            return
        seen.add(url)
        title = str(item.get("title") or url).strip()
        snippet = str(item.get("snippet") or item.get("content") or item.get("description") or "").strip()
        results.append({"title": title, "url": url, "snippet": snippet})

    choices = data.get("choices") or []
    message = {}
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}

    # a. message.search_results
    for item in message.get("search_results") or []:
        _add(item)

    # b. 顶层 search_results
    for item in data.get("search_results") or []:
        _add(item)

    # c. tool_calls 内嵌 server-side 结果
    for tc in message.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        if str(tc.get("type") or "").lower() != "web_search" and "web_search" not in str(tc.get("id") or "").lower():
            continue
        # arguments 可能携带查询词，但 server-side 搜索的实际结果在响应其他字段；
        # 若 tool_calls 里直接带 search_results 也一并提取
        args = tc.get("arguments")
        if isinstance(args, dict):
            for item in args.get("search_results") or []:
                _add(item)

    # d. citations（纯 URL 数组）
    for cit in (message.get("citations") or data.get("citations") or []):
        if isinstance(cit, str) and cit.strip():
            url = cit.strip()
            if url in seen:
                continue
            seen.add(url)
            results.append({"title": url, "url": url, "snippet": ""})
        elif isinstance(cit, dict):
            _add(cit)

    return results[: max(1, min(20, int(max_results)))]


def _build_payload(
    query: str,
    max_results: int,
    freshness: str,
    content_types: str,
    model: str,
    messages_extra: list | None = None,
) -> dict:
    """构造 OpenAI 兼容的 Chat Completions 请求体。"""
    system_prompt = _build_prompt(query, freshness, content_types, max_results)
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    if messages_extra:
        messages.extend(messages_extra)
    messages.append({"role": "user", "content": query})
    return {
        "model": model,
        "messages": messages,
        "tools": [{"type": "web_search"}],
        "stream": False,
        "max_tokens": 2048,
    }


def _parse_response(data: dict, max_results: int) -> list[dict]:
    """解析 xAI 响应：先尝试提取搜索结果，失败则给出带响应片段的明确异常。"""
    results = _extract_search_results(data, max_results)
    if results:
        return results

    # 无结果：提取错误信息或响应片段，抛出明确异常供编排层转兜底
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"Grok 返回错误: {data['error']}")
    snippet = str(data)[:400]
    raise RuntimeError(
        "Grok 响应中未找到搜索结果（search_results/citations/tool_calls 均缺失）。"
        f"原始响应片段: {snippet}"
    )


async def search(
    query: str,
    max_results: int = 5,
    freshness: str = "",
    content_types: str = "",
    plugin_config: dict | None = None,
    provider_settings: dict | None = None,
    timeout: int = 30,
) -> list[dict]:
    """调用 Grok 官方 Web Search，返回 [{title, url, snippet}]。

    无 key 或调用失败时抛出异常——smartsearch_fallback 编排层会捕获异常并
    自动记录日志、转用兜底引擎，不会中断整体搜索流程。
    """
    api_key = _resolve_api_key(plugin_config)
    if not api_key:
        raise ValueError(
            "Grok 引擎需要 API Key：请在插件 WebUI 配置页填写 grok_api_key，"
            "或设置环境变量 XAI_API_KEY。"
            "xAI Key 在 https://console.x.ai 获取，需充值，无免费额度。"
        )

    base_url, model = _resolve_settings(plugin_config)
    endpoint = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = _build_payload(query, max_results, freshness, content_types, model)

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(endpoint, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()

    # 首次请求若模型只返回 web_search tool_calls（两步协议形态），
    # 把 assistant 消息回传并发第二次请求，让服务端完成搜索后返回带结果的最终响应。
    choices = data.get("choices") or []
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        tool_calls = message.get("tool_calls") or []
        has_web_search_call = any(
            isinstance(tc, dict) and str(tc.get("type") or "").lower() == "web_search"
            for tc in tool_calls
        )
        if has_web_search_call:
            extra_messages = [message]
            for tc in tool_calls:
                if isinstance(tc, dict) and str(tc.get("type") or "").lower() == "web_search":
                    extra_messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id") or "",
                        "content": "",  # server-side 工具：无需客户端构造内容，服务端自动填充
                    })
            payload2 = _build_payload(query, max_results, freshness, content_types, model, extra_messages)
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp2 = await client.post(endpoint, headers=headers, json=payload2)
                resp2.raise_for_status()
                data = resp2.json()

    return _parse_response(data, max_results)
