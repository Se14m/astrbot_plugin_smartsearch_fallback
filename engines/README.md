# 自定义搜索引擎开发指南

smartsearch_fallback 支持**自由添加任意搜索引擎**：只要按下面的接口规范写一个
`.py` 文件放进本目录（`engines/`），重启（热重载）插件后即可在 WebUI 中把
`primary_engine` / `fallback_engine` 配置为该引擎名。

已内置引擎：`bocha`（优先，默认）、`anysearch`（兜底，默认）。
本目录自带自定义引擎：`duckduckgo`（免费无 key）、`serpapi`（真实 Google 结果，需 key）。

---

## 1. 自动发现机制

- 插件启动 / 热重载时扫描 `engines/` 目录下所有 `.py` 文件（**不以 `_` 开头**）。
- 每个文件通过 `ENGINE_NAME` 变量注册一个引擎；文件名只用于加载，**引擎名以
  `ENGINE_NAME` 为准**。
- 加载失败或缺少必要接口的文件会被跳过并输出 WARNING 日志，不影响其他引擎。
- 引擎注册表 = 内置（bocha / anysearch）+ 自定义（engines/ 目录）。配置引擎名时
  **大小写不敏感**（如 `SerpAPI` 与 `serpapi` 等价）。
- ⚠️ 新增 / 修改引擎后必须**热重载插件**（AStrBot WebUI → 插件管理 → 重启本插件），
  引擎缓存才会刷新。

## 2. 接口规范

每个引擎文件必须提供两样东西：

```python
ENGINE_NAME = "my_engine"   # str，用于 primary_engine / fallback_engine 配置

async def search(
    query: str,                 # 搜索关键词
    max_results: int = 5,       # 期望结果数（1~20）
    freshness: str = "",        # 时间过滤："day"/"week"/"month"/"year"，空为不限
    content_types: str = "",    # 内容类型："web"/"news"/"doc"，空为通用
    plugin_config: dict | None = None,   # 插件配置（_conf_schema.json 全部字段，原样透传）
    provider_settings: dict | None = None,  # AStrBot 全局 Provider 设置（可读 bocha 等 key）
    timeout: int = 30,          # 请求超时秒数
) -> list[dict]:
    """返回 [{"title": str, "url": str, "snippet": str}, ...]"""
```

要点：

- **返回格式固定**为 `[{title, url, snippet}]`，url 缺失或重复的条目会被过滤。
- **失败时抛异常**：编排层会捕获异常 → 记 WARNING 日志 → 自动转兜底引擎，
  不会中断整体搜索；两个引擎都失败时才返回错误信息。
- 结果不足时编排层会自动用兜底引擎补足并做 URL 归一化交叉验证（✓ 标记）。

## 3. 密钥读取（三种方式）

1. **插件配置项（推荐）**：在 `_conf_schema.json` 中新增一个字段（如
   `"my_engine_api_key"`），用户可在 WebUI 配置页填写，插件会原样透传到
   `plugin_config`，引擎内用 `plugin_config.get("my_engine_api_key")` 读取。
2. **环境变量**：`os.environ.get("MY_ENGINE_API_KEY")`，无需改配置。
3. 不要硬编码密钥进代码文件。

## 4. 最小实现模板

```python
import httpx

ENGINE_NAME = "my_engine"

async def search(query, max_results=5, freshness="", content_types="",
                 plugin_config=None, provider_settings=None, timeout=30):
    # 1) 取 key
    key = (plugin_config or {}).get("my_engine_api_key") or ""
    # 2) 调 API（httpx 已在依赖中）
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get("https://example.com/search",
                                params={"q": query, "key": key})
        resp.raise_for_status()
        data = resp.json()
    # 3) 统一为 [{title, url, snippet}]
    out = []
    for item in data.get("results", []):
        out.append({"title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "snippet": item.get("snippet", "")})
    return out[:max_results]
```

复制 `engines/duckduckgo.py` 作为起点亦可，它自带完整注释。

## 5. 添加引擎完整步骤

1. 在 `engines/` 下新建 `xxx.py`（或以 `duckduckgo.py` / `serpapi.py` 为模板复制）。
2. 设置 `ENGINE_NAME` 并实现 `async def search(...)`。
3. 如需 key：在 `_conf_schema.json` 增加对应字段（参照 `serpapi_api_key`）。
4. AStrBot WebUI → 插件管理 → 重启 smartsearch_fallback 插件（热重载）。
5. WebUI 配置页：把 `primary_engine` / `fallback_engine` 填为新引擎名，填入 key。
6. 发一条搜索消息验证；查日志确认 `smartsearch_fallback 已加载自定义引擎: xxx`。

## 6. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 日志提示 `未知搜索引擎: xxx` | 引擎名拼错，或没热重载（引擎未注册）。`可用引擎:` 后是当前全部引擎列表 |
| 日志提示 `加载自定义引擎 xxx.py 失败` | 文件语法错误或缺少 `ENGINE_NAME` / `search`，修好后热重载 |
| 改了引擎代码不生效 | `_custom_engine_cache` 是模块级缓存，必须热重载插件 |
| 两个引擎都失败 | 日志有对应 WARNING；检查 key、配额、网络与端点可达性 |
