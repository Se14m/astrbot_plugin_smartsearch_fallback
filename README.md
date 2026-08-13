# smartsearch_fallback

![SmartSearch 智能搜索封面](assets/cover.png)

**可配置优先/兜底引擎的智能搜索工具插件（AStrBot）**

> 当前版本 **v1.7.1** · 适配 AStrBot `>=4.16,<5`

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

插件启用后，AStrBot WebUI 的插件管理页会自动显示以下配置（按分组展示）：

### 引擎选择

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `primary_engine` | string | `bocha` | **优先搜索引擎**。第一优先级，先用它搜。可填 `bocha` / `anysearch` / `serpapi` / `grok` / `bing`（免 Key，国内可直连）/ `duckduckgo`（免 Key，国内通常连不上），或 `engines/` 目录下自定义引擎的 `ENGINE_NAME`。填错引擎名该路直接报错并自动落到兜底链 |
| `fallback_engine` | string | `anysearch` | **兜底搜索引擎**。优先引擎结果不足（少于 `min_quality_results`）或整体失败时，按逗号分隔顺序逐个尝试，第一个成功返回的即作为兜底。推荐国内稳定组合：`bing,serpapi` |

### API Key（方案 A：每个引擎独立 Key 槽，互不复用）

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `bocha_api_key` | string | 空 | **Bocha 引擎密钥**。仅当任一引擎选 `bocha` 时生效。留空自动读取 Provider 设置的 `websearch_bocha_key`（支持多 Key 轮换）；填写后本插件 Key 优先 |
| `anysearch_api_key` | string | 空 | **Anysearch 引擎密钥**。仅当任一引擎选 `anysearch` 时生效。留空走匿名访问（额度有限、易触发限流）；填写官方 Key 可显著提升稳定性 |
| `serpapi_api_key` | string | 空 | **SerpAPI 引擎密钥**。仅当任一引擎选 `serpapi` 时生效。在 https://serpapi.com 注册获取；留空回退读取环境变量 `SERPAPI_API_KEY` |
| `grok_api_key` | string | 空 | **Grok 引擎密钥（xAI Web Search）**。仅当任一引擎选 `grok` 时生效。在 https://console.x.ai 获取，需充值无免费额度；留空回退读取环境变量 `XAI_API_KEY`。只认本槽位，不复用其它 Key 槽 |
| `grok_base_url` | string | `https://api.x.ai/v1` | **Grok 引擎 API 端点**。使用 OpenAI 兼容中转时填中转地址（形如 `https://xxx/v1`）；留空读取环境变量 `XAI_BASE_URL`。第三方中转若服务端不支持 `web_search` 工具透传，grok 引擎会静默失败 |
| `grok_model` | string | `grok-4-fast` | **Grok 引擎模型名**。可选 `grok-4` / `grok-3.5` 等（需账户可用）；留空读取环境变量 `XAI_MODEL`。实测 `grok-4-fast` 响应快但判断偶有误判，追求准确选 `grok-4` |
| `fallback_api_key` | string | 空 | **自定义引擎通用密钥（预留槽位）**。方案 A 后专用 Key 槽已拆分，本槽位仅保留给 `engines/` 目录下的自定义引擎读取，普通配置无需填写 |

### 质量与超时

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `min_quality_results` | int | 3 | **兜底引擎触发阈值**。优先引擎结果数低于该值（或为 0）时自动调用兜底引擎补足并交叉验证；设为 0 表示任何情况都强制走兜底流程 |
| `max_results` | int | 5 | **单次搜索默认返回条数**（1-20） |
| `timeout` | int | 6 | **单引擎请求超时（秒）**。方案 A 收紧后的默认值，配合下方 `total_timeout` 总闸控制整体耗时 |
| `total_timeout` | int | 30 | **整体搜索总超时（秒）**。主引擎 → LLM 判质量 → 兜底引擎 → 二次验证的硬性总闸：累计超时则强制返回已得的部分结果，任何情况下搜索 ≤ 该值返回 |

### LLM 验证

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `llm_verifier_enabled` | bool | `false` | **启用 LLM 结果验证**。开启后合并结果交由 LLM 做相关性验证，剔除低相关条目并附验证摘要；默认关闭，验证失败自动跳过不影响结果 |
| `llm_verifier_provider_id` | string | 空 | **验证模型**。下拉复用 AStrBot 已配置 Provider；实测推荐 `deepseek-v4-flash`（比 grok-4 更快更省且判断准确）；留空自动用当前会话默认 Provider |
| `llm_verifier_prompt` | text | 见默认 | **验证提示词模板**。支持占位符 `{query}` 与 `{results_json}`，要求模型只输出严格 JSON；修改后需重启或热重载生效 |
| `llm_verifier_max_results` | int | 5 | **单次最多验证条数**。控制验证成本，只验证合并结果前 N 条 |
| `llm_verifier_timeout` | int | 10 | **验证请求超时（秒）**。超时则跳过 LLM 校验，不影响搜索结果 |

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
    api_key = (plugin_config or {}).get("fallback_api_key", "")  # 可选：读插件配置（自定义引擎通用 Key 槽）
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

**bocha**：插件配置 `bocha_api_key` > Provider 设置 `websearch_bocha_key`（多 Key 轮换）

**anysearch**：插件配置 `anysearch_api_key` > 匿名访问（额度有限）。方案 A 前旧字段 `fallback_api_key` 兼容回退

**serpapi**：插件配置 `serpapi_api_key` > 环境变量 `SERPAPI_API_KEY`。方案 A 前旧字段 `fallback_api_key` 兼容回退

**grok**：插件配置 `grok_api_key` > 环境变量 `XAI_API_KEY`（端点/模型同理：`grok_base_url` > `XAI_BASE_URL`，`grok_model` > `XAI_MODEL`）。方案 A 起不再复用 `fallback_api_key`

**bing**：免 Key，直接解析 bing.com/cn.bing.com 搜索结果页，国内网络可直连，无需配置。

**duckduckgo**：免 Key，国内网络通常无法直连，失败时自动转兜底。

**自定义引擎（engines/ 目录）**：可自由读取插件配置任意字段，通用槽位约定为 `fallback_api_key`。

## 常见问题

**Q: 两个搜索源都失败了？**
检查优先/兜底引擎对应的 Key 配置（如 Provider 设置中的 `websearch_bocha_key`、插件配置页的 `bocha_api_key` / `anysearch_api_key` / `serpapi_api_key` / `grok_api_key`），再确认对应端点可达。工具返回的错误信息会指出失败的引擎名。

**Q: 如何完全关闭兜底引擎？**
将 `fallback_engine` 配置为与 `primary_engine` 相同的引擎名即可（此时只使用单一引擎，不再补足交叉验证）。

**Q: 配置的引擎名提示"未知搜索引擎"？**
工具会返回当前可用引擎列表。确认引擎名拼写（`ENGINE_NAME`，小写），自定义引擎需确认文件已放入 `engines/` 目录且格式正确，并重载插件。

**Q: 搜索太慢？**
插件自带 `total_timeout` 全局总闸（默认 30 秒），整个流程到点强制返回部分结果，不会无限等待。若仍需提速：可降低 `timeout`（引擎）与 `llm_verifier_timeout`（LLM 验证，当前默认 6s / 10s），或调低 `total_timeout`（例如 20 秒）。当优先引擎结果充足时不会触发兜底引擎，速度不受影响。

## 依赖

- `httpx`（见 `requirements.txt`）

## 更新日志

- **v1.7.1**：以 v1.7.1 重新发布（商店侧 v1.7.0 版本号已占用、无法复用；内容与 v1.7.0 一致）
- **v1.7.0**：方案 A 2.0 Key 槽位拆分与描述全面重写。① **Key 槽位拆分**：`fallback_api_key` 拆分为 `anysearch_api_key` / `serpapi_api_key` / `grok_api_key` 三个专用槽，各引擎 Key 互不复用（旧配置中的 `fallback_api_key` 值已由迁移脚本转移到对应专用槽，代码层保留旧字段兼容回退）；② **移除 google 引擎**：删除 `engines/google.py` 与 `google_api_key` / `google_cx` 配置项（国内直连不稳定、收益低），主/兜底引擎不再支持 `google`；③ **配置介绍全部推翻重写**：`_conf_schema.json` 全部 18 个配置项的 description / hint 从零重写并分组（引擎选择 / API Key / 质量与超时 / LLM 验证），README 同步重写配置表格与优先级说明
- **v1.6.5**：接入 AstrBot Plugin Pages WebUI。新增总览、真实搜索测试、引擎编排、健康检查和设置保存页面；新增插件 Web API；配置保存后同步应用到当前运行实例；API Key 仅返回配置状态和掩码；保留 `_conf_schema.json` 兼容旧版 AstrBot。
- **v1.6.0**：方案 B+A 性能改造。① **新增 `total_timeout` 全局总闸（默认 30s）**：整个搜索流程（主引擎 → LLM 判质量 → 兜底引擎 → 二次验证）串行累加最坏可达 120s，现由最外层 `asyncio.timeout` 硬性锁死 ≤30s，超时强制返回已收集的部分结果；② **阶段预算动态分配**：各阶段调用统一取 `min(原超时, 剩余预算)`，慢 provider 被预算掐死而非吃掉全局时间；二次验证在总预算剩余不足 5s 时跳过新 LLM 调用、复用判质量结果；③ **方案 A 配置收紧**：`timeout` 默认 30→6s、`llm_verifier_timeout` 默认 30→10s（最坏 6+10+6+10=32s，由总闸兜底），正常场景速度不变
- **v1.5.1**：新增免 key 引擎 `bing`（国内可直连，解析 bing.com/cn.bing.com 网页结果）与 `google`（优先官方 Custom Search JSON API，未配 key 时降级网页版解析）；新增 `google_api_key` / `google_cx` 配置项（兼容环境变量 `GOOGLE_API_KEY` / `GOOGLE_CX`）
- **v1.5.0**：适配插件市场发布，配置项精简统一。① 兜底引擎 Key 合并：`anysearch_api_key` / `serpapi_api_key` 合并为 `fallback_api_key`（anysearch/serpapi/自定义兜底引擎通用，旧字段自动兼容）；删除 `anysearch_endpoint`；② LLM 验证配置简化：`llm_verifier_model` / `llm_verifier_api_base` / `llm_verifier_api_key` 替换为 `llm_verifier_provider_id`（WebUI 下拉直接复用 AStrBot 已配置 Provider，无需再填 Key/Base URL/模型名）；③ 版本号统一为语义化 `1.5.0`（main.py / metadata.yaml / README 三处一致）
- **v1.4.0**：优化验证与兜底逻辑。① 启用 LLM 结果验证后，验证模型**先**判断主引擎结果质量，判定质量不佳（相关结果少于 `min_quality_results`）才调用兜底引擎做交叉搜索验证，未触发兜底时复用首次校验结果避免重复调用 LLM；② 配置页精简：删除 `anysearch_endpoint` 选项，「优先/兜底搜索引擎 API Key」选项调转顺序并精简描述；③ 兜底引擎可设置 `anysearch` 或 `serpapi`，`serpapi` 引擎支持环境变量 `SERPAPI_API_KEY` 或插件配置 `serpapi_api_key`
- **v1.2.0**：引擎可配置化。新增 `primary_engine` / `fallback_engine` 配置项；新增 `engines/` 自定义引擎自动发现机制，支持任意搜索方式；交叉验证与输出来源标记参数化；附带免 key 示例引擎 `engines/duckduckgo.py`
- **v1.1.0**：新增 `_conf_schema.json` 适配 AStrBot 插件 WebUI 配置页；bocha/anysearch key、endpoint、阈值、超时均可在配置页调整
- **v1.0.0**：首个版本，bocha 优先 + anysearch 交叉验证，适配 AStrBot v4.27.2 FunctionTool API
