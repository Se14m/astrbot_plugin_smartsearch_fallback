# smartsearch_fallback

![SmartSearch 智能搜索封面](assets/cover.png)

**可配置优先/兜底引擎的智能搜索工具插件（AStrBot）**

> 当前版本 **v1.6.5** · 适配 AStrBot `>=4.16,<5`

默认「bocha 优先 + anysearch 兜底」，当优先引擎结果不足或失败时，自动调用兜底引擎补足，并对两路结果做交叉验证，输出带来源标记（`✓` 表示双引擎验证一致）的可靠结果。**优先/兜底引擎均可配置为任意搜索方式**（内置 bocha / anysearch，或放入 `engines/` 目录的自定义引擎）。

## 特性

- 🔀 **引擎可配置**：`primary_engine` / `fallback_engine` 可在 WebUI 配置为任意可用引擎，不限于 bocha / anysearch
- 🧩 **自定义引擎热插拔**：在插件 `engines/` 目录放一个 `.py` 文件即可接入任意搜索方式（SerpAPI、Bing、DuckDuckGo、内部 API…）
- 🔍 **双引擎容错**：优先引擎挂了自动降级到兜底引擎，不漏结果
- ✅ **交叉验证**：两路来源 URL 一致的条目标记 `✓`，提升可信度
- 🧠 **可选 LLM 结果验证**：开启后调用一个 LLM 模型对搜索结果逐条做相关性/可信度校验，剔除低质量结果；验证模型的提示词可在 WebUI 自定义（支持 `{query}` / `{results_json}` 占位符）
- 🔑 **多 key 轮换**：自动读取 AStrBot 全局 Provider 设置中的 `websearch_bocha_key`（支持多个 key 轮换），也可在插件配置中单独指定
- 🛠️ **两个工具**：`smart_search`（单查询）+ `smart_search_batch`（最多 5 个查询并行）
- 📋 **WebUI 可视化配置**：安装后由 AStrBot Plugin Pages 自动挂载，提供总览、搜索测试、引擎编排和设置页面；旧版 AStrBot 仍可使用 `_conf_schema.json` 原生配置页
- ⌨️ **手动命令**：`/smartsearch <query>` 可随时手动测试

## WebUI

在支持 Plugin Pages 的 AstrBot 版本中，安装并启用插件后，AstrBot 会自动发现 `pages/smartsearch/index.html`。入口不是左侧一级菜单，而是：`插件管理` → `已安装插件` → 点击本插件卡片 → 在插件详情的“页面”组件中点击“打开页面”。页面通过插件自身注册的 Web API 读取和保存配置，不需要额外启动 Node 服务或占用新的端口。

如果详情页没有出现“页面”组件，请先卸载旧版插件（可保留配置），确认 `data/plugins/astrbot_plugin_smartsearch_fallback` 目录已被删除，再安装本版本并重启 AstrBot。AstrBot 安装器会拒绝覆盖同名目录，不能通过重复上传实现更新。

页面提供以下功能：

- 查看插件版本、已注册引擎、优先/兜底策略和搜索次数
- 检查引擎配置状态，API Key 只返回掩码和配置状态
- 使用当前插件配置执行真实搜索并复制结果
- 在线修改引擎、结果阈值、超时和 LLM 验证设置

如果当前 AstrBot 版本不支持 Plugin Pages Web API，插件会保留原有 `_conf_schema.json` 配置方式，不影响原有搜索工具。

## 工作原理

```
用户提问
   │
   ▼
smart_search 工具
   │
   ├─① primary_engine 搜索（默认 bocha，多 key 轮换）
   │     │
   │     ├─ [可选] ② LLM 结果验证先行（llm_verifier_enabled）
   │     │     验证模型先判断主引擎结果质量
   │     │       ├─ 相关结果 ≥ min_quality_results → 直接输出（✓ 标记来源，剔除低相关条目）
   │     │       └─ 判定质量不佳 / 相关结果不足 / 主引擎报错
   │     │             │
   │     │             ▼
   │     │       ③ fallback_engine 兜底（默认 anysearch，可设 serpapi）
   │     │             │
   │     │             ▼
   │     │       ④ 交叉验证 → 合并输出（✓ = 双引擎一致）
   │     │             │
   │     │             ▼
   │     │       ⑤ 对最终合并结果再次 LLM 校验，剔除低质量结果
   │     │
   │     └─ 未启用 LLM 验证：按数量阈值（结果 < min_quality_results / 0 条 / 报错）触发兜底
   │
   ▼
返回带来源标记的文本
```

## LLM 结果验证（可选）

在插件 WebUI 配置页（或 `_conf_schema.json`）中开启：

| 配置项 | 说明 |
|---|---|
| `llm_verifier_enabled` | 开关，默认关闭 |
| `llm_verifier_provider_id` | **（推荐）直接复用 AStrBot 已配置的 LLM Provider**：WebUI 中为下拉选择器，选中后无需填写任何 Key / Base URL / 模型名，验证请求直接走该 Provider；留空则自动按「当前会话 Provider → 第一个可用 Provider → 当前使用中 Provider」回退 |
| `llm_verifier_prompt` | 自定义验证提示词，支持 `{query}`（查询）和 `{results_json}`（候选结果 JSON）两个占位符；缺失时自动补全。要求模型输出严格 JSON：`{"results":[{"index":0,"relevant":true,"reason":"..."}],"summary":"..."}` |
| `llm_verifier_max_results` | 单次送入验证的条数上限，默认 5 |
| `llm_verifier_timeout` | 验证请求超时秒数，默认 10 |

行为说明：

- 开启后，合并结果会先经过 LLM 校验，`relevant=false` 的条目被剔除，结果文本中追加一行 `🧠 LLM 校验(模型名): 剔除 N 条低相关结果；<summary>`
- **全杀保护**：若验证模型把全部结果判为无关，则放弃本次验证结果（保留原结果），避免误杀
- 验证失败（超时/解析失败）静默降级，不影响原搜索结果
- 调用方式与 `astrbot_plugin_qq_group_daily_analysis` 一致：优先 `context.llm_generate(chat_provider_id=...)` 官方 API 直接调用 AStrBot 已配置模型；失败自动回退 Provider 实例方法（`text_chat` / `text_chat_async` / `llm_chat`）
- 验证调用使用 `temperature=0`，保证输出稳定

## 安装

1. 将 `astrbot_plugin_smartsearch_fallback` 目录放入 AStrBot 的 `data/plugins/` 下
2. 在 AStrBot WebUI →「插件管理」中启用插件
3. 在插件配置页按需填写配置（见下节）
4. 重启 AStrBot 或重新加载插件

## WebUI 配置项说明

插件启用后，AStrBot WebUI 的插件管理页会自动显示以下配置：

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `primary_engine` | string | `bocha` | 优先搜索引擎。可填 `bocha` / `anysearch` / `serpapi` / `bing`（免 key，国内可直连）/ `google`（免 key 降级网页版，建议配 key）/ `duckduckgo`（免 key），或 `engines/` 目录下自定义引擎的 `ENGINE_NAME` |
| `fallback_engine` | string | `anysearch` | 兜底搜索引擎。优先引擎结果不足或验证质量不佳时启用；可填 `bocha` / `anysearch` / `serpapi` / `bing` / `google` / `duckduckgo`（均为免 key 或可选 key）等可用引擎名；填与 `primary_engine` 相同的值 = 关闭兜底 |
| `bocha_api_key` | string | 空 | **优先搜索引擎 API Key（可选）**。留空自动读取 Provider 设置中的 `websearch_bocha_key`；填写后本插件 key 优先 |
| `fallback_api_key` | string | 空 | **兜底引擎通用 API Key（可选）**。anysearch 留空走匿名访问（额度有限）；serpapi 必填（或用环境变量 `SERPAPI_API_KEY`）；自定义引擎亦可读取此字段 |
| `google_api_key` | string | 空 | **Google Custom Search JSON API Key（可选）**。选择 `google` 引擎时配合 `google_cx` 使用；留空读取环境变量 `GOOGLE_API_KEY`，再留空则降级为 google.com 网页版解析（国内网络通常连不通，建议配 key） |
| `google_cx` | string | 空 | **Google Custom Search 搜索引擎 ID（可选）**。配合 `google_api_key`；留空读取环境变量 `GOOGLE_CX` |
| `min_quality_results` | int | 3 | 优先引擎结果数低于该值（或为 0）时触发兜底引擎补足；设为 0 表示总是走补足流程 |
| `max_results` | int | 5 | 单次搜索默认返回结果数（1-20） |
| `timeout` | int | 6 | 搜索请求超时时间（秒）。已按方案 A 收紧默认值，配合下方 `total_timeout` 总闸使用 |
| `total_timeout` | int | 30 | **整体搜索总超时（秒）**。整个流程（主引擎 → LLM 判质量 → 兜底引擎 → 二次验证）的硬性总闸：各阶段按剩余预算动态分配，合计超时则强制返回已收集的部分结果，任何情况下搜索 ≤ 该值返回 |

> 修改配置后在 WebUI 中保存并重新加载插件即可生效。新增/修改 `engines/` 目录下的自定义引擎后需重启插件（或重启 AStrBot）才会被加载。

## 自定义引擎（支持任意搜索方式）

### 快速开始

插件内置了一个免 key 的示例引擎 `engines/duckduckgo.py`。把它配置为优先引擎试试：

```
primary_engine = duckduckgo
fallback_engine = anysearch   （或 bocha）
```

保存并重载插件后，`smart_search` 即走 DuckDuckGo → anysearch 的编排。

### 编写自己的引擎

在插件 `engines/` 目录下新建任意 `.py` 文件（不以 `_` 开头），实现统一接口即可被自动发现：

```python
"""我的引擎：例如接入 SerpAPI / Bing / 内部搜索服务。"""
import httpx

ENGINE_NAME = "serpapi"   # 用于 primary_engine / fallback_engine 配置，须唯一

async def search(query, max_results=5, freshness="", content_types="",
                 plugin_config=None, provider_settings=None, timeout=30):
    """统一引擎接口。返回 [{"title": str, "url": str, "snippet": str}]"""
    api_key = (plugin_config or {}).get("fallback_api_key", "")  # 可选：读插件配置（兜底引擎通用 Key）
    # ... 调用你的搜索服务，组装成上面的列表结构 ...
    return results
```

接口约定：

| 项 | 说明 |
|---|---|
| `ENGINE_NAME` | 引擎名（str），配置 `primary_engine` / `fallback_engine` 时填它 |
| `search()` | 必须为 `async` 函数，返回 `[{title, url, snippet}]`；抛异常会被上层捕获并按"引擎不可用"处理 |
| `plugin_config` | 插件全部配置（`_conf_schema.json` 中定义的项），可自行约定读取自定义 key 字段 |
| `provider_settings` | AStrBot 全局 Provider 设置（含 `websearch_bocha_key` 等），可用于复用全局密钥 |

> 提示：若自定义引擎需要专属配置项（如 API Key），在 `_conf_schema.json` 中追加对应字段即可，插件会原样透传给 `search()`。

## 使用方式

### 1. LLM 自动调用（推荐）

插件注册了 `smart_search` / `smart_search_batch` 两个 FunctionTool，AStrBot 的 LLM 在对话中会按需自动调用，无需额外指令。

### 2. 手动命令

```
/smartsearch 明日方舟 2026 新干员
```

直接输出搜索结果，用于测试连通性与引擎配置是否生效。

## 配置优先级

**bocha key**：插件配置 `bocha_api_key` > Provider 设置 `websearch_bocha_key`（多 key 轮换）

**兜底引擎（anysearch/serpapi）**：插件配置 `fallback_api_key`；anysearch 留空走匿名访问，serpapi 留空可读环境变量 `SERPAPI_API_KEY`（旧字段 `anysearch_api_key` / `serpapi_api_key` 仍兼容）

**google 引擎**：插件配置 `google_api_key` + `google_cx`（或环境变量 `GOOGLE_API_KEY` / `GOOGLE_CX`）；两者齐全走官方 Custom Search JSON API，否则降级为 google.com 网页版解析（国内网络直连 Google 通常超时，会按异常转兜底）。注册入口：https://programmablesearchengine.google.com/ 创建搜索项目获取 cx，https://console.cloud.google.com/ 开启 Custom Search API 获取 key。

**bing 引擎**：免 key，直接解析 bing.com/cn.bing.com 搜索结果页，国内网络可直连，无需配置。

## 常见问题

**Q: 两个搜索源都失败了？**
检查优先/兜底引擎对应的 key 配置（如 Provider 设置中的 `websearch_bocha_key`、插件配置页的 `fallback_api_key` / `bocha_api_key`），再确认对应端点可达。工具返回的错误信息会指出失败的引擎名。

**Q: 如何完全关闭兜底引擎？**
将 `fallback_engine` 配置为与 `primary_engine` 相同的引擎名即可（此时只使用单一引擎，不再补足交叉验证）。

**Q: 配置的引擎名提示"未知搜索引擎"？**
工具会返回当前可用引擎列表。确认引擎名拼写（`ENGINE_NAME`，小写），自定义引擎需确认文件已放入 `engines/` 目录且格式正确，并重载插件。

**Q: 搜索太慢？**
插件自带 `total_timeout` 全局总闸（默认 30 秒），整个流程到点强制返回部分结果，不会无限等待。若仍需提速：可降低 `timeout`（引擎）与 `llm_verifier_timeout`（LLM 验证，当前默认 6s / 10s），或调低 `total_timeout`（例如 20 秒）。当优先引擎结果充足时不会触发兜底引擎，速度不受影响。

## 依赖

- `httpx`（见 `requirements.txt`）

## 更新日志

- **v1.6.5**：接入 AstrBot Plugin Pages WebUI。新增总览、真实搜索测试、引擎编排、健康检查和设置保存页面；新增插件 Web API；配置保存后同步应用到当前运行实例；API Key 仅返回配置状态和掩码；保留 `_conf_schema.json` 兼容旧版 AstrBot。
- **v1.6.0**：方案 B+A 性能改造。① **新增 `total_timeout` 全局总闸（默认 30s）**：整个搜索流程（主引擎 → LLM 判质量 → 兜底引擎 → 二次验证）串行累加最坏可达 120s，现由最外层 `asyncio.timeout` 硬性锁死 ≤30s，超时强制返回已收集的部分结果；② **阶段预算动态分配**：各阶段调用统一取 `min(原超时, 剩余预算)`，慢 provider 被预算掐死而非吃掉全局时间；二次验证在总预算剩余不足 5s 时跳过新 LLM 调用、复用判质量结果；③ **方案 A 配置收紧**：`timeout` 默认 30→6s、`llm_verifier_timeout` 默认 30→10s（最坏 6+10+6+10=32s，由总闸兜底），正常场景速度不变
- **v1.5.1**：新增免 key 引擎 `bing`（国内可直连，解析 bing.com/cn.bing.com 网页结果）与 `google`（优先官方 Custom Search JSON API，未配 key 时降级网页版解析）；新增 `google_api_key` / `google_cx` 配置项（兼容环境变量 `GOOGLE_API_KEY` / `GOOGLE_CX`）
- **v1.5.0**：适配插件市场发布，配置项精简统一。① 兜底引擎 Key 合并：`anysearch_api_key` / `serpapi_api_key` 合并为 `fallback_api_key`（anysearch/serpapi/自定义兜底引擎通用，旧字段自动兼容）；删除 `anysearch_endpoint`；② LLM 验证配置简化：`llm_verifier_model` / `llm_verifier_api_base` / `llm_verifier_api_key` 替换为 `llm_verifier_provider_id`（WebUI 下拉直接复用 AStrBot 已配置 Provider，无需再填 Key/Base URL/模型名）；③ 版本号统一为语义化 `1.5.0`（main.py / metadata.yaml / README 三处一致）
- **v1.4.0**：优化验证与兜底逻辑。① 启用 LLM 结果验证后，验证模型**先**判断主引擎结果质量，判定质量不佳（相关结果少于 `min_quality_results`）才调用兜底引擎做交叉搜索验证，未触发兜底时复用首次校验结果避免重复调用 LLM；② 配置页精简：删除 `anysearch_endpoint` 选项，「优先/兜底搜索引擎 API Key」选项调转顺序并精简描述；③ 兜底引擎可设置 `anysearch` 或 `serpapi`，`serpapi` 引擎支持环境变量 `SERPAPI_API_KEY` 或插件配置 `serpapi_api_key`
- **v1.2.0**：引擎可配置化。新增 `primary_engine` / `fallback_engine` 配置项；新增 `engines/` 自定义引擎自动发现机制，支持任意搜索方式；交叉验证与输出来源标记参数化；附带免 key 示例引擎 `engines/duckduckgo.py`
- **v1.1.0**：新增 `_conf_schema.json` 适配 AStrBot 插件 WebUI 配置页；bocha/anysearch key、endpoint、阈值、超时均可在配置页调整
- **v1.0.0**：首个版本，bocha 优先 + anysearch 交叉验证，适配 AStrBot v4.27.2 FunctionTool API
