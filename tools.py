"""smartsearch_fallback: 可配置优先/兜底搜索引擎的智能搜索工具。

策略（引擎可配置，默认保持原有行为）：
1. 优先调用 primary_engine（默认 bocha，读 AStrBot provider_settings 中的 websearch_bocha_key，自动 key 轮换）。
2. 若优先引擎结果为空 / 少于 min_quality_results，自动调用 fallback_engine（默认 anysearch）补足。
3. 两路结果做 URL 归一化交叉验证，输出带 verified 标记的合并结果。

引擎可扩展性：
- 内置引擎: bocha / anysearch（由本文件实现）。
- 自定义引擎: 在插件目录 engines/ 下放置 .py 文件即可自动发现，支持任意搜索方式
  （DuckDuckGo、SerpAPI、Bing、本地数据库、内部 API 等），只需实现统一接口：
      ENGINE_NAME = "my_engine"              # 引擎名，用于 primary_engine/fallback_engine 配置
      async def search(query, max_results, freshness, content_types,
                       plugin_config, provider_settings, timeout) -> list[dict]:
          # 返回 [{"title": str, "url": str, "snippet": str}]
  可参考 engines/duckduckgo.py 示例。

适配 AStrBot 当前版本 API：FunctionTool 子类实现 async def call(context, **kwargs)。
"""

import asyncio
import importlib.util
import json
import os
import re
import uuid
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx
from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

from astrbot.api import FunctionTool, logger

DEFAULT_BOCHA_ENDPOINT = "https://api.bochaai.com/v1/web-search"
DEFAULT_ANYSEARCH_ENDPOINT = "https://api.anysearch.com/mcp"

ENGINES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "engines")

# LLM 结果验证默认提示词。占位符 {query} / {results_json} 会被实际查询与候选结果 JSON 替换。
DEFAULT_LLM_VERIFIER_PROMPT = """你是搜索质量验证器。逐条判断下方候选结果是否与查询主题相关，并评估来源。只输出 JSON，不要任何多余文字。

判定规则（从宽，宁漏勿杀）：
1. relevant：内容与查询沾边即 true。自媒体/论坛/个人博客只要相关也判 true，绝不因来源非官方判 false。
2. source_type：official=政府/机构/公司官网/百科；media=新闻媒体；blog=个人博客/自媒体；forum=社区/论坛；unknown=无法判断。
3. credibility：官方/主流媒体=high；相关且有实质内容=medium；标题党/内容空洞/明显不相关=low。
4. reason：不超过 20 字，说明相关点或不相关原因。

输出格式（只输出此 JSON）：
{"results": [{"index": 0, "relevant": true, "source_type": "official", "credibility": "high", "reason": "直接解答查询"}], "summary": "≤40字整体结论"}

查询：{query}
候选结果：
{results_json}"""

# ----------------------------- 基础工具 -----------------------------


def _normalize_url(url: str) -> str:
    """URL 归一化，用于交叉验证去重。"""
    try:
        p = urlparse(url.strip().lower())
        host = p.hostname or ""
        if host.startswith("www."):
            host = host[4:]
        path = p.path.rstrip("/")
        return f"{host}{path}"
    except Exception:
        return url.strip().lower()


class KeyRotator:
    """轮换多个 key，失败自动切换到下一个。"""

    def __init__(self, keys: list[str]):
        self.keys = [k for k in keys if k]
        self._idx = 0
        if not self.keys:
            raise ValueError("未配置任何 API key")

    def get(self) -> str:
        key = self.keys[self._idx % len(self.keys)]
        self._idx += 1
        return key


async def _bocha_search(
    provider_settings: dict,
    query: str,
    max_results: int = 5,
    freshness: str = "noLimit",
    summary: bool = True,
    api_key_override: str = "",
    timeout: int = 30,
) -> list[dict]:
    """调用 bocha web-search，自动 key 轮换。返回 [{title,url,snippet}]。"""
    keys = provider_settings.get("websearch_bocha_key", []) or []
    if api_key_override:
        keys = [api_key_override] + list(keys)  # 插件配置的 key 优先
    if not keys:
        raise RuntimeError(
            "未配置 bocha API key (provider_settings.websearch_bocha_key 或插件配置 bocha_api_key)"
        )
    payload = {"query": query, "summary": summary, "count": max_results, "freshness": freshness, "answer": False}
    kr = KeyRotator(keys)
    last_err: Optional[Exception] = None
    for _ in range(max(1, len(keys))):
        try:
            headers = {"Authorization": f"Bearer {kr.get()}", "Content-Type": "application/json"}
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(DEFAULT_BOCHA_ENDPOINT, headers=headers, json=payload)
            if resp.status_code in (401, 403, 429) or resp.status_code >= 500:
                continue  # 换下一个 key
            resp.raise_for_status()
            data = resp.json()
            pages = (data.get("data") or {}).get("webPages", {}).get("value", [])
            results = []
            for p in pages:
                results.append(
                    {
                        "title": p.get("name", ""),
                        "url": p.get("url", ""),
                        "snippet": p.get("summary", p.get("snippet", "")),
                    }
                )
            return results
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning(f"bocha 搜索失败(尝试下一个key): {e}")
    raise RuntimeError(f"bocha 搜索最终失败: {last_err}")


async def _anysearch_search(
    query: str,
    max_results: int = 5,
    freshness: str = "",
    content_types: str = "",
    api_key: str = "",
    endpoint: str = DEFAULT_ANYSEARCH_ENDPOINT,
    timeout: int = 30,
) -> list[dict]:
    """调用 anysearch 的 search 工具（JSON-RPC）。返回 [{title,url,snippet}]。"""
    arguments: dict[str, Any] = {"query": query, "max_results": max_results}
    if freshness:
        arguments["freshness"] = freshness
    if content_types:
        arguments["content_types"] = content_types
    request_id = str(uuid.uuid4())
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": "search", "arguments": arguments},
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(endpoint, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    content = (data.get("result") or {}).get("content") or []
    results: list[dict] = []
    for item in content:
        text = item.get("text") or ""
        try:
            parsed = json.loads(text) if text.startswith(("{", "[")) else None
        except json.JSONDecodeError:
            parsed = None
        if parsed is None:
            continue
        items = parsed if isinstance(parsed, list) else parsed.get("results", parsed.get("items", [parsed]))
        for it in items:
            if not isinstance(it, dict):
                continue
            title = it.get("title") or it.get("name") or ""
            url = it.get("url") or it.get("link") or ""
            snippet = it.get("snippet") or it.get("summary") or it.get("description") or ""
            if url:
                results.append({"title": str(title), "url": str(url), "snippet": str(snippet)})
    return results


def _dedup(results: list[dict]) -> list[dict]:
    """引擎内按归一化 URL 去重，保留第一条。"""
    seen: set[str] = set()
    out: list[dict] = []
    for r in results:
        norm = _normalize_url(r["url"])
        if norm and norm in seen:
            continue
        seen.add(norm)
        out.append(r)
    return out


def _merge_and_verify(primary: list[dict], secondary: list[dict], primary_name: str, fallback_name: str) -> dict:
    """交叉验证合并：URL 归一化后求交集标记 verified，两路内部均先去重。"""
    primary = _dedup(primary)
    secondary = _dedup(secondary)
    verified: list[dict] = []
    primary_only: list[dict] = []
    sec_by_norm = {}
    for r in secondary:
        sec_by_norm.setdefault(_normalize_url(r["url"]), r)
    for r in primary:
        norm = _normalize_url(r["url"])
        if norm in sec_by_norm:
            rr = dict(r)
            rr["source"] = f"{primary_name}+{fallback_name}"
            rr["verified"] = True
            verified.append(rr)
        else:
            rr = dict(r)
            rr["source"] = primary_name
            rr["verified"] = False
            primary_only.append(rr)
    pri_norms = {_normalize_url(p["url"]) for p in primary}
    secondary_only = [
        dict(r, source=fallback_name, verified=False) for r in secondary if _normalize_url(r["url"]) not in pri_norms
    ]
    return {"verified": verified, "primary_only": primary_only, "secondary_only": secondary_only}


def _format_output(query: str, merged: dict, engines_used: str, used_fallback: bool, llm_info: Optional[dict] = None) -> str:
    """组装给 LLM 的可读文本输出。llm_info 为可选 LLM 验证结果信息。"""
    all_items = merged["verified"] + merged["primary_only"] + merged["secondary_only"]
    lines = [f"搜索结果 (query: {query})", f"引擎: {engines_used}"]
    if not all_items:
        lines.append("未找到结果。")
        return "\n".join(lines)
    lines.append(f"共 {len(all_items)} 条结果:")
    _ST_MAP = {"official": "官方/权威", "media": "新闻媒体", "blog": "个人博客/自媒体", "forum": "社区/论坛", "unknown": "来源未知"}
    _CR_MAP = {"high": "高", "medium": "中", "low": "低"}
    for i, r in enumerate(all_items, 1):
        tag = "✓" if r.get("verified") else ""
        src = r.get("source", "?")
        lines.append(f"[{i}] {r['title']} {tag}")
        lines.append(f"    {r['url']}")
        snippet = (r.get("snippet") or "")[:180]
        if snippet:
            lines.append(f"    {snippet}")
        cred_suffix = ""
        if r.get("llm_source_type"):
            st = _ST_MAP.get(r.get("llm_source_type"), r.get("llm_source_type"))
            cr = _CR_MAP.get(r.get("llm_credibility", "medium"), r.get("llm_credibility"))
            if r.get("llm_source_type") == "official" and r.get("llm_credibility") == "high":
                cred_suffix = " · 官方权威"
            else:
                warn = "⚠️ 请核实" if r.get("llm_credibility") == "low" else "注意核实"
                cred_suffix = f" · {st} · 可信度:{cr} · {warn}"
        lines.append(f"    (来源: {src}{cred_suffix})")
    if used_fallback and not merged["verified"]:
        lines.append("提示: 两路引擎结果无重合，建议进一步核实。")
    if llm_info:
        parts = []
        if llm_info.get("dropped"):
            parts.append(f"剔除 {llm_info['dropped']} 条低相关结果")
        if llm_info.get("summary"):
            parts.append(str(llm_info["summary"]))
        if parts:
            lines.append(f"🧠 LLM 校验({llm_info.get('model', 'LLM')}): " + "；".join(parts))
    return "\n".join(lines)


def _provider_settings(context) -> dict:
    """从 AstrAgentContext 中读取 provider_settings，兼容 event 缺失场景。"""
    try:
        agent_ctx = context.context
        event = getattr(agent_ctx, "event", None)
        umo = getattr(event, "unified_msg_origin", None) if event else None
        cfg = agent_ctx.context.get_config(umo=umo)
        return (cfg or {}).get("provider_settings", {}) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"读取 provider_settings 失败: {e}")
        return {}


# ----------------------------- 引擎注册表 -----------------------------
# 统一引擎接口：
#   async def search(query, max_results, freshness, content_types,
#                    plugin_config, provider_settings, timeout) -> list[dict]
# 返回 [{title, url, snippet}]。


async def _bocha_engine(
    query: str,
    max_results: int = 5,
    freshness: str = "",
    content_types: str = "",
    plugin_config: Optional[dict] = None,
    provider_settings: Optional[dict] = None,
    timeout: int = 30,
) -> list[dict]:
    """内置 bocha 引擎（统一接口包装）。"""
    plugin_config = plugin_config or {}
    provider_settings = provider_settings or {}
    bocha_key = str(plugin_config.get("bocha_api_key") or "").strip()
    return await _bocha_search(
        provider_settings, query, max_results, freshness or "noLimit",
        api_key_override=bocha_key, timeout=timeout,
    )


async def _anysearch_engine(
    query: str,
    max_results: int = 5,
    freshness: str = "",
    content_types: str = "",
    plugin_config: Optional[dict] = None,
    provider_settings: Optional[dict] = None,
    timeout: int = 30,
) -> list[dict]:
    """内置 anysearch 引擎（统一接口包装）。"""
    plugin_config = plugin_config or {}
    anysearch_key = str(
        plugin_config.get("anysearch_api_key")
        or plugin_config.get("fallback_api_key") or ""
    ).strip()
    return await _anysearch_search(
        query, max_results, freshness, content_types,
        api_key=anysearch_key, timeout=timeout,
    )


BUILTIN_ENGINES: dict[str, Callable] = {
    "bocha": _bocha_engine,
    "anysearch": _anysearch_engine,
}

_custom_engine_cache: Optional[dict[str, Callable]] = None


def _load_custom_engines() -> dict[str, Callable]:
    """扫描 engines/ 目录，动态加载自定义引擎。每个 .py 文件定义一个引擎：
    ENGINE_NAME + async def search(...)。加载失败或缺少接口时跳过并告警。"""
    global _custom_engine_cache
    if _custom_engine_cache is not None:
        return _custom_engine_cache
    registry: dict[str, Callable] = {}
    if not os.path.isdir(ENGINES_DIR):
        _custom_engine_cache = registry
        return registry
    for fn in sorted(os.listdir(ENGINES_DIR)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        mod_name = fn[:-3]
        mod_path = os.path.join(ENGINES_DIR, fn)
        try:
            spec = importlib.util.spec_from_file_location(f"smartsearch_custom_engine_{mod_name}", mod_path)
            if spec is None or spec.loader is None:
                logger.warning(f"自定义引擎 {fn} 无法创建加载器，已跳过")
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"加载自定义引擎 {fn} 失败: {e}")
            continue
        name = str(getattr(mod, "ENGINE_NAME", "") or mod_name).strip().lower()
        search_fn = getattr(mod, "search", None)
        if not name or not callable(search_fn):
            logger.warning(f"自定义引擎 {fn} 缺少 ENGINE_NAME 或 async def search(...)，已跳过")
            continue
        registry[name] = search_fn
        logger.info(f"smartsearch_fallback 已加载自定义引擎: {name} ({fn})")
    _custom_engine_cache = registry
    return registry


def get_engine_registry() -> dict[str, Callable]:
    """获取完整引擎注册表（内置 + 自定义）。"""
    registry = dict(BUILTIN_ENGINES)
    registry.update(_load_custom_engines())
    return registry


# ----------------------------- LLM 结果验证（可选） -----------------------------
# 启用 llm_verifier_enabled 后，用 LLM 模型对合并结果做相关性验证。
# 调用方式（参考 astrbot_plugin_qq_group_daily_analysis 的 LLM 设置）：
#   通过 llm_verifier_provider_id 选择 AStrBot 已配置的 Provider（留空自动用当前会话默认 Provider），
#   调用 context.llm_generate(chat_provider_id=...)，失败时回退 Provider 实例方法。
# 任何失败都静默跳过，绝不影响主搜索流程。


def _build_llm_verify_messages(query: str, results: list[dict], prompt_tpl: str) -> list[dict]:
    """构造 LLM 验证消息。模板需含 {query} 与 {results_json} 占位符，缺失时自动补齐。"""
    payload = [
        {
            "index": i,
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": (r.get("snippet") or "")[:200],
        }
        for i, r in enumerate(results)
    ]
    if "{query}" not in prompt_tpl or "{results_json}" not in prompt_tpl:
        prompt_tpl += chr(10).join(["", "查询：{query}", "候选结果：", "{results_json}"])
    prompt = prompt_tpl.replace("{query}", query).replace(
        "{results_json}", json.dumps(payload, ensure_ascii=False, indent=2)
    )
    return [
        {"role": "system", "content": "你只输出 JSON，不输出任何其他内容。"},
        {"role": "user", "content": prompt},
    ]


def _parse_llm_verifier_output(text: str, n: int) -> Optional[dict]:
    """解析 LLM 验证输出，兼容 markdown 代码块与多余文本。返回 {"verdict": {...}, "summary": str}。"""
    if not text:
        return None
    t = text.strip()
    if "```" in t:
        m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
        if m:
            t = m.group(1).strip()
    start, end = t.find("{"), t.rfind("}")
    if start >= 0 and end > start:
        t = t[start : end + 1]
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    summary = str(data.get("summary") or "").strip()
    verdict: dict[int, dict] = {}
    for item in data.get("results") or []:
        if not isinstance(item, dict) or "index" not in item:
            continue
        try:
            idx = int(item["index"])
        except (TypeError, ValueError):
            continue
        if 0 <= idx < n:
            verdict[idx] = {
                "relevant": bool(item.get("relevant", True)),
                "source_type": str(item.get("source_type") or "unknown").strip().lower(),
                "credibility": str(item.get("credibility") or "medium").strip().lower(),
                "reason": str(item.get("reason") or "").strip(),
            }
    return {"verdict": verdict, "summary": summary}


def _get_star_context(context):
    """多层解析出真正可用的 Star Context（兼容 AstrAgentContext / 手动命令壳 _Ctx）。

    FunctionTool 的 call(context) 传入的可能是 AstrAgentContext（.context 指向 Star Context），
    手动 /smartsearch 命令则传入 _Ctx（.context.context 指向 Star Context），这里统一逐层下钻。
    """
    seen = set()
    cur = context
    for _ in range(5):
        if cur is None or id(cur) in seen:
            break
        seen.add(id(cur))
        if hasattr(cur, "get_provider_by_id") and hasattr(cur, "llm_generate"):
            return cur
        nxt = getattr(cur, "context", None)
        if nxt is None or nxt is cur:
            break
        cur = nxt
    return context


def _get_umo(context) -> Optional[str]:
    """尝试从事件链中提取 unified_msg_origin，用于解析当前会话 Provider。"""
    try:
        if hasattr(context, "event") and getattr(context.event, "unified_msg_origin", None):
            return context.event.unified_msg_origin
        agent_ctx = getattr(context, "context", None)
        event = getattr(agent_ctx, "event", None) if agent_ctx is not None else None
        if event is not None:
            return getattr(event, "unified_msg_origin", None)
        if agent_ctx is not None:
            return getattr(agent_ctx, "unified_msg_origin", None)
    except Exception:  # noqa: BLE001
        pass
    return None


def _extract_llm_text(resp) -> Optional[str]:
    """从 LLMResponse 中提取文本，兼容 completion_text（新版）与 result（旧版）。"""
    if resp is None:
        return None
    for attr in ("completion_text", "result"):
        t = getattr(resp, attr, None)
        if t:
            return str(t)
    s = str(resp)
    return s if s.strip() else None


def _resolve_llm_provider_id(star, configured_id: str, umo: Optional[str]) -> Optional[str]:
    """解析可用的 LLM Provider ID（多级回退，参考 qq_group_daily_analysis）：

    1) 配置的 llm_verifier_provider_id → 2) 当前会话 Provider → 3) 第一个可用 Provider → 4) 当前使用中 Provider。
    每一级都通过 get_provider_by_id 验证存在后才采用。
    """
    candidates: list[tuple[str, str]] = []
    if configured_id:
        candidates.append(("配置的 llm_verifier_provider_id", configured_id))
    if umo:
        try:
            sid = star.get_current_chat_provider_id(umo=umo)
            if sid:
                candidates.append(("当前会话 Provider", sid))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"获取当前会话 Provider ID 失败: {e}")
    try:
        all_p = star.get_all_providers() or []
        for p in all_p:
            try:
                pid = p.meta().id
                if pid:
                    candidates.append(("第一个可用 Provider", pid))
                    break
            except Exception:  # noqa: BLE001
                continue
    except Exception as e:  # noqa: BLE001
        logger.debug(f"枚举 Provider 失败: {e}")
    try:
        up = star.get_using_provider(umo=umo)
        if up is not None:
            try:
                pid = up.meta().id
                if pid:
                    candidates.append(("当前使用中 Provider", pid))
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    seen: set[str] = set()
    for desc, pid in candidates:
        if not pid or pid in seen:
            continue
        seen.add(pid)
        try:
            p = star.get_provider_by_id(provider_id=pid)
            if p is not None:
                logger.info(f"LLM 验证 Provider 选择: {desc} → {pid}")
                return pid
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Provider {pid} 校验失败: {e}")
    return None


async def _llm_chat_provider(
    context, query: str, results: list[dict], prompt_tpl: str, timeout: int,
    configured_provider_id: str = "",
) -> Optional[str]:
    """调用 AStrBot 已配置的 LLM Provider 验证结果。

    参考 astrbot_plugin_qq_group_daily_analysis 的 Provider 选择与调用方式：
    优先使用 context.llm_generate(chat_provider_id=...) 官方标准 API（直接复用 AStrBot 已配置模型），
    失败后回退到 Provider 实例方法（text_chat/text_chat_async/llm_chat）以兼容旧版本。
    """
    star = _get_star_context(context)
    umo = _get_umo(context)
    pid = _resolve_llm_provider_id(star, configured_provider_id, umo)

    messages = _build_llm_verify_messages(query, results, prompt_tpl)
    system_prompt = messages[0]["content"] if messages else None
    user_prompt = messages[-1]["content"] if messages else None

    # 1) 官方标准 API：context.llm_generate（直接调用 AStrBot 已配置模型）
    if pid and user_prompt:
        try:
            resp = await asyncio.wait_for(
                star.llm_generate(
                    chat_provider_id=pid,
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                ),
                timeout=timeout,
            )
            text = _extract_llm_text(resp)
            if text:
                logger.info(f"LLM 验证成功（llm_generate, provider={pid}）")
                return text
        except asyncio.TimeoutError:
            logger.warning(f"LLM 验证 llm_generate 超时（{timeout}s），尝试回退调用方式")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"LLM 验证 llm_generate 失败: {e}，尝试回退调用方式")

    # 2) 兼容旧版：Provider 实例方法
    provider = None
    try:
        provider = star.get_provider_by_id(provider_id=pid) if pid else None
    except Exception:  # noqa: BLE001
        provider = None
    if provider is None:
        try:
            provider = star.get_using_provider(umo=umo)
        except Exception:  # noqa: BLE001
            provider = None
    if provider is None:
        return None
    prompt = chr(10) * 2 + chr(10).join(f"{m['role']}: {m['content']}" for m in messages)
    for method in ("text_chat", "text_chat_async", "llm_chat"):
        fn = getattr(provider, method, None)
        if not callable(fn):
            continue
        try:
            resp = await asyncio.wait_for(fn(prompt), timeout=timeout)
        except TypeError:
            try:
                resp = await asyncio.wait_for(
                    fn(prompt, session_id="smartsearch_llm_verify"), timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(f"LLM Provider {method} 调用超时（{timeout}s），已跳过 LLM 验证")
                continue
            except Exception as e:  # noqa: BLE001
                logger.warning(f"LLM Provider {method} 调用失败: {e}")
                continue
        except asyncio.TimeoutError:
            logger.warning(f"LLM Provider {method} 调用超时（{timeout}s），已跳过 LLM 验证")
            continue
        except Exception as e:  # noqa: BLE001
            logger.warning(f"LLM Provider {method} 调用失败: {e}")
            continue
        text = _extract_llm_text(resp)
        if text:
            return text
    return None


async def _llm_verify(context, query: str, results: list[dict], plugin_config: dict) -> Optional[dict]:
    """调用 LLM 模型验证搜索结果相关性。任何失败都返回 None，不阻断主流程。"""
    try:
        cfg = plugin_config or {}
        if not cfg.get("llm_verifier_enabled"):
            return None
        max_n = max(1, int(cfg.get("llm_verifier_max_results") or 5))
        timeout = max(5, int(cfg.get("llm_verifier_timeout") or 30))
        prompt_tpl = str(cfg.get("llm_verifier_prompt") or "").strip() or DEFAULT_LLM_VERIFIER_PROMPT
        subset = results[:max_n]
        text = await _llm_chat_provider(
            context, query, subset, prompt_tpl, timeout,
            configured_provider_id=str(cfg.get("llm_verifier_provider_id") or "").strip(),
        )
        if not text:
            return None
        parsed = _parse_llm_verifier_output(text, len(subset))
        if parsed is None or not parsed.get("verdict"):
            logger.warning("LLM 验证输出无法解析，已跳过 LLM 校验（不影响搜索结果）")
            return None
        return parsed
    except Exception as e:  # noqa: BLE001
        logger.warning(f"LLM 验证失败，已跳过（不影响搜索结果）: {e}")
        return None


async def _apply_llm_verification(context, query: str, merged: dict, plugin_config: dict, precomputed_verdict: Optional[dict] = None) -> Optional[dict]:
    """对合并结果做 LLM 相关性验证并剔除低相关条目。失败返回 None，不影响主流程。
    precomputed_verdict 传入时直接复用（其索引须与 all_items 顺序一致），不重复调用 LLM。"""
    all_items = merged["verified"] + merged["primary_only"] + merged["secondary_only"]
    if not all_items:
        return None
    if precomputed_verdict is not None:
        out = precomputed_verdict
    else:
        out = await _llm_verify(context, query, all_items, plugin_config)
    if not out or not out.get("verdict"):
        return None
    kept, dropped = [], []
    for i, item in enumerate(all_items):
        v = out["verdict"].get(i)
        if v is None or v["relevant"]:
            # 附加来源类型与可信度标注（供结果展示用；低可信度不剔除，仅标注）
            item["llm_source_type"] = v.get("source_type", "unknown") if v else "unknown"
            item["llm_credibility"] = v.get("credibility", "medium") if v else "medium"
            item["llm_reason"] = v.get("reason", "") if v else ""
            kept.append(item)
        else:
            dropped.append(item)
    if not kept:  # 保护：LLM 误杀全部时回退，不剔除
        kept, dropped = all_items, []
    dropped_ids = {id(d) for d in dropped}
    merged["verified"] = [r for r in merged["verified"] if id(r) not in dropped_ids]
    merged["primary_only"] = [r for r in merged["primary_only"] if id(r) not in dropped_ids]
    merged["secondary_only"] = [r for r in merged["secondary_only"] if id(r) not in dropped_ids]
    model = str(plugin_config.get("llm_verifier_provider_id") or "").strip() or "AStrBot当前LLM"
    return {"dropped": len(dropped), "summary": out.get("summary") or "", "model": model}


# ----------------------------- 核心编排 -----------------------------


async def _smart_search_impl(
    context,
    query: str,
    max_results: int = 5,
    freshness: str = "",
    content_types: str = "",
    plugin_config: dict | None = None,
) -> dict:
    """核心编排：primary_engine 优先，不足则 fallback_engine 补足并交叉验证。
    方案B：全局 total_timeout 总闸 + 阶段预算动态分配。
    任何情况 ≤total_timeout 秒返回；正常场景耗时不变，慢 provider 被预算掐死。"""
    plugin_config = plugin_config or {}
    max_results = max(1, min(20, int(max_results)))
    timeout = int(plugin_config.get("timeout") or 30)
    # ---- 方案B：全局总闸 + 阶段预算动态分配 ----
    total_budget = max(5.0, float(plugin_config.get("total_timeout") or 30))
    _loop = asyncio.get_running_loop()
    _deadline = _loop.time() + total_budget

    def _budget(floor: float = 2.0) -> float:
        """当前阶段可用预算（秒）：剩余时间，至少保留 floor 秒用于收尾。"""
        return max(floor, _deadline - _loop.time())

    def _budget_left() -> float:
        """距离总闸的真实剩余时间（秒）。"""
        return _deadline - _loop.time()

    min_quality_raw = plugin_config.get("min_quality_results")
    min_quality = int(min_quality_raw) if min_quality_raw is not None else 3
    primary_name = str(plugin_config.get("primary_engine") or "bocha").strip().lower()
    # fallback_engine 支持逗号分隔的多级兜底链，如 "bing,serpapi"：
    # 依次尝试，第一个成功返回结果的引擎作为兜底；全部失败且主引擎无结果时报错。
    fallback_chain = [
        x.strip().lower()
        for x in str(plugin_config.get("fallback_engine") or "anysearch").split(",")
        if x.strip()
    ]
    fallback_name = fallback_chain[0] if fallback_chain else ""

    registry = get_engine_registry()
    primary_fn = registry.get(primary_name)
    available = ", ".join(sorted(registry.keys())) or "(无)"
    if primary_fn is None:
        return {
            "status": "error",
            "text": (
                f"未知搜索引擎: {primary_name}。可用引擎: {available}。"
                "可在插件 WebUI 配置 primary_engine / fallback_engine，"
                "或在插件 engines/ 目录添加自定义引擎。"
            ),
        }
    unknown_chain = [fb for fb in fallback_chain if fb not in registry]
    if unknown_chain:
        return {
            "status": "error",
            "text": (
                f"未知兜底搜索引擎: {', '.join(unknown_chain)}。可用引擎: {available}。"
                "可在插件 WebUI 配置 primary_engine / fallback_engine（支持逗号分隔多级兜底，如 bing,serpapi）。"
            ),
        }

    provider_settings = _provider_settings(context)
    # 状态变量在外层初始化，供总闸超时后拼"部分结果"使用
    used_fallback = False
    engines_used = primary_name
    primary: list = []
    secondary: list = []
    merged: dict = {"verified": [], "primary_only": [], "secondary_only": []}
    llm_info = None
    llm_enabled = bool(plugin_config.get("llm_verifier_enabled"))
    pre_verdict = None
    need_fallback = False
    fallback_reason = ""

    try:
        # ============ 最外层保险丝：整个流程硬性 ≤total_budget 秒 ============
        async with asyncio.timeout(total_budget):
            # 主引擎预算预留 3s 兜底窗口：即使 timeout 配置 ≥ total_timeout，
            # 兜底引擎也永远有执行机会（防御 timeout 调大后兜底失效的边界）
            engine_budget = min(timeout, max(1.0, _budget() - 3))
            try:
                primary = await asyncio.wait_for(
                    primary_fn(
                        query, max_results, freshness, content_types,
                        plugin_config, provider_settings, engine_budget,
                    ),
                    timeout=engine_budget,
                )
                primary = _dedup(primary)  # 去重后再做 LLM 校验，保证索引与合并结果一致
            except asyncio.TimeoutError:
                logger.warning(f"{primary_name} 阶段超时（预算 {engine_budget:.1f}s），转 {fallback_name}")
                primary = []
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{primary_name} 不可用，转 {fallback_name}: {e}")
                primary = []

            if min_quality <= 0:
                # min_quality=0：总是走补足流程（配置语义不变）
                need_fallback = True
                fallback_reason = "min_quality_results=0，总是走补足流程"
            elif not llm_enabled:
                # 未启用 LLM 校验：沿用数量阈值判断
                need_fallback = len(primary) == 0 or len(primary) < min_quality
                fallback_reason = "主引擎结果数量不足"
            elif not primary:
                need_fallback = True
                fallback_reason = "主引擎无结果"
            else:
                # 先由验证模型判断主引擎结果质量，判定不佳才调用兜底引擎交叉搜索验证
                try:
                    pre_verdict = await asyncio.wait_for(
                        _llm_verify(context, query, primary, plugin_config),
                        timeout=_budget(),
                    )
                except asyncio.TimeoutError:
                    logger.warning("LLM 判质量超时（总预算内），回退数量阈值判断")
                    pre_verdict = None
                if pre_verdict is None:
                    need_fallback = len(primary) == 0 or len(primary) < min_quality
                    fallback_reason = "LLM 校验不可用，回退数量阈值判断"
                else:
                    relevant_cnt = sum(
                        1 for v in pre_verdict.get("verdict", {}).values() if v.get("relevant")
                    )
                    need_fallback = len(primary) == 0 or relevant_cnt < min_quality
                    fallback_reason = (
                        f"LLM 校验判定主引擎相关结果仅 {relevant_cnt} 条（阈值 {min_quality}），质量不佳"
                    )
            if need_fallback and primary_name not in fallback_chain:
                # 多级兜底链：依次尝试 fallback_chain 中的引擎，第一个成功返回结果的即作为兜底
                used_name = ""
                for fb in fallback_chain:
                    fb_fn = registry.get(fb)
                    fb_budget = min(timeout, _budget())
                    if fb_budget < 1.5:
                        logger.warning(f"总预算不足（剩 {fb_budget:.1f}s），跳过剩余兜底引擎 {fb}")
                        break
                    try:
                        candidate = await asyncio.wait_for(
                            fb_fn(
                                query, max_results, freshness, content_types,
                                plugin_config, provider_settings, fb_budget,
                            ),
                            timeout=fb_budget,
                        )
                        candidate = _dedup(candidate)
                        if candidate:
                            secondary = candidate
                            used_name = fb
                            used_fallback = True
                            engines_used = f"{primary_name} + {fb}" if primary else fb
                            break
                        logger.warning(f"{fb} 返回空结果，尝试下一个兜底引擎")
                    except asyncio.TimeoutError:
                        logger.warning(f"{fb} 阶段超时（预算 {fb_budget:.1f}s），尝试下一个兜底引擎")
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"{fb} 补足失败: {e}，尝试下一个兜底引擎")
                if not used_fallback and not primary:
                    return {
                        "status": "error",
                        "text": (
                            f"所有搜索源({primary_name}/{'/'.join(fallback_chain)})均失败。"
                            "请检查对应引擎的 key、网络与配置（插件 WebUI 配置页）。"
                        ),
                    }
                fallback_name = used_name or fallback_chain[0]
            if used_fallback:
                logger.info(f"触发兜底引擎 {fallback_name}（原因: {fallback_reason}），并入 {len(secondary)} 条结果交叉验证")
            merged = _merge_and_verify(primary, secondary, primary_name, fallback_name)
            # LLM 结果验证（可选：llm_verifier_enabled=true 时启用，失败不影响主流程）
            if llm_enabled:
                if used_fallback or pre_verdict is None:
                    if _budget_left() < 5:
                        # 预算不足 5s：跳过新 LLM 调用，直接复用判质量结果（省一次 LLM；
                        # 索引安全：all_items 前段顺序与 primary 一致，secondary_only 部分未覆盖则保留）
                        logger.warning(f"总预算剩余 {_budget_left():.1f}s，跳过二次 LLM 验证，复用首次校验结果")
                        if pre_verdict is not None:
                            llm_info = await _apply_llm_verification(
                                context, query, merged, plugin_config, precomputed_verdict=pre_verdict
                            )
                    else:
                        try:
                            llm_info = await asyncio.wait_for(
                                _apply_llm_verification(context, query, merged, plugin_config),
                                timeout=_budget(),
                            )
                        except asyncio.TimeoutError:
                            logger.warning("LLM 二次验证超时（总预算内），跳过")
                else:
                    # 质量达标未触发兜底：复用首次校验结果，避免重复调用 LLM
                    llm_info = await _apply_llm_verification(
                        context, query, merged, plugin_config, precomputed_verdict=pre_verdict
                    )
    except asyncio.TimeoutError:
        # ============ 总闸触发：返回已收集的部分结果 ============
        logger.warning(f"整体搜索超时（>{total_budget:.0f}s），返回部分结果")
        if not (merged["verified"] or merged["primary_only"] or merged["secondary_only"]):
            merged = _merge_and_verify(primary, secondary, primary_name, fallback_name)
    text = _format_output(query, merged, engines_used, used_fallback, llm_info)
    return {
        "status": "ok",
        "query": query,
        "engine": engines_used,
        "cross_verified": len(merged["verified"]) > 0,
        "count": len(merged["verified"]) + len(merged["primary_only"]) + len(merged["secondary_only"]),
        "results": merged,
        "llm_verified": bool(llm_info),
        "llm_dropped": (llm_info or {}).get("dropped", 0),
        "text": text,
    }


# ----------------------------- 工具定义（新版 API） -----------------------------


@pydantic_dataclass
class SmartSearchTool(FunctionTool):
    """单查询：primary_engine 优先，必要时 fallback_engine 交叉验证。"""

    plugin_config: dict = Field(default_factory=dict)
    """插件配置（来自 _conf_schema.json，由 main.py 注入）。"""

    name: str = "smart_search"
    description: str = (
        "智能搜索：按插件配置的优先引擎搜索，当结果不足时自动调用兜底引擎补足并交叉验证"
        "（默认 bocha 优先 + anysearch 兜底，可在插件配置中更换任意引擎）。"
        "适合需要可靠、可交叉核实的最新信息查询。返回结果带来源标记，✓ 表示双引擎验证一致。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词或自然语言问题"},
                "max_results": {"type": "integer", "description": "返回结果数量，默认5，最大20"},
                "freshness": {"type": "string", "description": "时间过滤: day/week/month/year，可选"},
                "content_types": {"type": "string", "description": "内容类型过滤(引擎相关): 如 web,news,doc，可选"},
            },
            "required": ["query"],
        }
    )

    async def call(self, context, **kwargs) -> str:
        query = kwargs.get("query", "")
        if not query:
            return "Error: 缺少 query 参数。"
        result = await _smart_search_impl(
            context,
            query,
            kwargs.get("max_results", 5),
            kwargs.get("freshness", ""),
            kwargs.get("content_types", ""),
            plugin_config=self.plugin_config,
        )
        return result["text"]


@pydantic_dataclass
class SmartSearchBatchTool(FunctionTool):
    """批量搜索：对多个查询并行执行 smart_search 逻辑。"""

    plugin_config: dict = Field(default_factory=dict)
    """插件配置（来自 _conf_schema.json，由 main.py 注入）。"""

    name: str = "smart_search_batch"
    description: str = (
        "批量智能搜索：同时对多个关键词执行 优先引擎 + 兜底引擎 交叉验证搜索。"
        "适合需要一次获取多个主题信息（如对比、清单、定时任务）的场景，最多5个查询。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "string",
                    "description": "多个查询，用逗号分隔或 JSON 数组字符串，最多5个",
                },
                "max_results": {"type": "integer", "description": "每个查询返回结果数量，默认5，最大10"},
                "freshness": {"type": "string", "description": "时间过滤: day/week/month/year，可选"},
            },
            "required": ["queries"],
        }
    )

    async def call(self, context, **kwargs) -> str:
        queries = kwargs.get("queries", "")
        if isinstance(queries, list):
            q_list = [str(q).strip() for q in queries if str(q).strip()]
        elif isinstance(queries, str) and queries.strip().startswith("["):
            try:
                q_list = [str(q).strip() for q in json.loads(queries) if str(q).strip()]
            except json.JSONDecodeError:
                q_list = [q.strip() for q in queries.split(",") if q.strip()]
        else:
            q_list = [q.strip() for q in str(queries).split(",") if q.strip()]
        q_list = q_list[:5]
        if not q_list:
            return "Error: 未提供有效查询。"
        max_results = kwargs.get("max_results", 5)
        freshness = kwargs.get("freshness", "")
        results = await asyncio.gather(
            *[_smart_search_impl(context, q, max_results, freshness, plugin_config=self.plugin_config) for q in q_list]
        )
        lines = [f"批量搜索完成，共 {len(results)} 组:", ""]
        for q, r in zip(q_list, results):
            lines.append(f"=== {q} ===")
            lines.append(r.get("text", "无结果"))
            lines.append("")
        return "\n".join(lines)
