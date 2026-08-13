from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request

from .tools import (
    SmartSearchBatchTool,
    SmartSearchTool,
    _smart_search_impl,
    get_engine_registry,
)

@register(
    "smartsearch_fallback",
    "smartsearch_fallback",
    "可配置优先/兜底引擎的智能搜索工具（内置 bocha/anysearch + 自定义引擎 + 可选 LLM 结果验证）",
    "1.7.1",
    "https://github.com/Se14m/astrbot_plugin_smartsearch_fallback",
)
class SmartSearchFallbackPlugin(Star):
    VERSION = "1.7.1"
    SECRET_FIELDS = ("bocha_api_key", "anysearch_api_key", "serpapi_api_key", "grok_api_key", "fallback_api_key")
    DEFAULTS = {
        "primary_engine": "bocha",
        "fallback_engine": "anysearch",
        "min_quality_results": 3,
        "max_results": 5,
        "timeout": 6,
        "total_timeout": 30,
        "llm_verifier_enabled": False,
        "llm_verifier_provider_id": "",
    }

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.plugin_config = dict(self.DEFAULTS)
        self.plugin_config.update(dict(self.config))
        self.stats = {"search_count": 0, "last_result": None}
        self.tools = [
            SmartSearchTool(plugin_config=self.plugin_config),
            SmartSearchBatchTool(plugin_config=self.plugin_config),
        ]
        self.context.add_llm_tools(*self.tools)
        self._register_web_api()
        engines = get_engine_registry()
        logger.info(
            "smartsearch_fallback 1.7.1 已注册: smart_search / smart_search_batch "
            f"(优先={self.plugin_config.get('primary_engine')}, "
            f"兜底={self.plugin_config.get('fallback_engine')}, "
            f"可用引擎={', '.join(sorted(engines)) or '无'})"
        )

    def _register_web_api(self):
        if not callable(getattr(self.context, "register_web_api", None)):
            logger.info("当前 AstrBot 版本不提供 Plugin Pages Web API，保留原生配置页")
            return
        routes = (
            ("stats", self._api_stats, ["GET"], "SmartSearch overview statistics"),
            ("settings", self._api_settings, ["GET"], "Read SmartSearch settings"),
            ("settings/save", self._api_save_settings, ["POST"], "Save SmartSearch settings"),
            ("engines", self._api_engines, ["GET"], "List SmartSearch engines"),
            ("search", self._api_search, ["POST"], "Run a SmartSearch query"),
            ("health-check", self._api_health_check, ["POST"], "Check SmartSearch configuration"),
        )
        for path, handler, methods, description in routes:
            self.context.register_web_api(
                f"/astrbot_plugin_smartsearch_fallback/{path}",
                handler,
                methods,
                description,
            )

    @staticmethod
    def _mask(value: Any) -> str:
        text = str(value or "")
        if not text:
            return ""
        return f"{'*' * min(8, max(4, len(text) - 2))}{text[-2:]}"

    def _public_settings(self) -> dict[str, Any]:
        data = dict(self.plugin_config)
        for field in self.SECRET_FIELDS:
            value = data.pop(field, "")
            data[f"{field}_configured"] = bool(str(value).strip())
            data[f"{field}_masked"] = self._mask(value)
        return data

    @staticmethod
    def _json_error(message: str, status: int = 400):
        return error_response(message, status_code=status)

    async def _api_stats(self):
        registry = get_engine_registry()
        return json_response(
            {
                "version": self.VERSION,
                "engine_count": len(registry),
                "engines": sorted(registry),
                "primary_engine": self.plugin_config.get("primary_engine", "bocha"),
                "fallback_engine": self.plugin_config.get("fallback_engine", "anysearch"),
                "llm_verifier_enabled": bool(self.plugin_config.get("llm_verifier_enabled")),
                "search_count": self.stats["search_count"],
                "last_result": self.stats["last_result"],
            }
        )

    async def _api_settings(self):
        return json_response(self._public_settings())

    async def _api_engines(self):
        registry = get_engine_registry()
        return json_response({"engines": sorted(registry)})

    async def _api_save_settings(self):
        body = await request.json(default={})
        if not isinstance(body, dict):
            return self._json_error("请求内容必须是 JSON 对象")
        next_config = dict(self.plugin_config)
        registry = get_engine_registry()
        for field in ("primary_engine", "fallback_engine"):
            if field in body:
                value = str(body[field] or "").strip().lower()
                names = [x.strip() for x in value.split(",") if x.strip()]
                if not names or any(n not in registry for n in names):
                    return self._json_error(f"未知搜索引擎: {value}")
                next_config[field] = value
        integer_rules = {
            "min_quality_results": (0, 10),
            "max_results": (1, 20),
            "timeout": (5, 120),
            "total_timeout": (5, 120),
        }
        for field, (minimum, maximum) in integer_rules.items():
            if field in body:
                try:
                    value = int(body[field])
                except (TypeError, ValueError):
                    return self._json_error(f"{field} 必须是数字")
                if not minimum <= value <= maximum:
                    return self._json_error(f"{field} 必须在 {minimum} 到 {maximum} 之间")
                next_config[field] = value
        if "llm_verifier_enabled" in body:
            next_config["llm_verifier_enabled"] = bool(body["llm_verifier_enabled"])
        if "llm_verifier_provider_id" in body:
            next_config["llm_verifier_provider_id"] = str(body["llm_verifier_provider_id"] or "").strip()
        for field in self.SECRET_FIELDS:
            value = body.get(field)
            if value is not None and str(value).strip() and not str(value).strip().startswith("***"):
                next_config[field] = str(value).strip()

        self.plugin_config.clear()
        self.plugin_config.update(next_config)
        if hasattr(self.config, "update"):
            self.config.update(next_config)
        save_config = getattr(self.config, "save_config", None)
        if callable(save_config):
            try:
                save_config(next_config)
            except TypeError:
                save_config()
        return json_response(self._public_settings())

    async def _api_search(self):
        body = await request.json(default={})
        if not isinstance(body, dict):
            return self._json_error("请求内容必须是 JSON 对象")
        query = str(body.get("query") or "").strip()
        if not query:
            return self._json_error("请输入搜索内容")
        try:
            max_results = int(body.get("max_results") or self.plugin_config.get("max_results", 5))
        except (TypeError, ValueError):
            return self._json_error("max_results 必须是数字")
        if not 1 <= max_results <= 20:
            return self._json_error("max_results 必须在 1 到 20 之间")
        result = await _smart_search_impl(
            self._fake_context(),
            query,
            max_results=max_results,
            freshness=str(body.get("freshness") or ""),
            content_types=str(body.get("content_types") or ""),
            plugin_config=self.plugin_config,
        )
        self.stats["search_count"] += 1
        self.stats["last_result"] = {
            "query": query,
            "count": result.get("count", 0),
            "status": result.get("status", "error"),
        }
        return json_response(result)

    async def _api_health_check(self):
        registry = get_engine_registry()
        checks = []
        for name in sorted(registry):
            configured = True
            message = "可调用"
            if name == "bocha":
                configured = bool(str(self.plugin_config.get("bocha_api_key") or "").strip()) or bool(
                    self._provider_settings_for_health()
                )
                message = "已配置 Key 或使用 AstrBot Provider" if configured else "缺少 Bocha Key"
            elif name in {"anysearch", "serpapi"}:
                configured = bool(str(self.plugin_config.get("fallback_api_key") or "").strip())
                message = "已配置插件 Key" if configured else "未配置插件 Key，可能依赖环境变量"
            checks.append({"name": name, "ok": configured, "message": message})
        return json_response({"checks": checks, "checked_at": "now"})

    def _provider_settings_for_health(self) -> dict:
        try:
            config = self.context.get_config()
            return (config or {}).get("provider_settings", {}) or {}
        except Exception:
            return {}

    @filter.command("smartsearch")
    async def smartsearch_cmd(self, event: AstrMessageEvent, query: str):
        """手动触发: /smartsearch <query>"""
        try:
            result = await SmartSearchTool(plugin_config=self.plugin_config).call(self._fake_context(), query=query)
            yield event.plain_result(result)
        except Exception as e:
            logger.error(f"smartsearch 手动调用失败: {e}")
            yield event.plain_result(f"搜索失败: {e}")

    def _fake_context(self):
        class _AgentCtx:
            context = self.context
            event = None

        class _Ctx:
            context = _AgentCtx()

        return _Ctx()
