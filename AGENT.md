# AGENT.md — ComfyUI Danbooru Gallery Plus

> 面向接手本仓库的 AI 助手 / 开发者：讲清**项目结构、每个文件的职责、数据怎么流动、改代码时的约定与坑**。
> 改代码前请先读第 9 节「不变量」和第 10 节「怎么验证」。

---

## 1. 项目是什么

单节点 ComfyUI 插件：在节点里直接浏览 **Danbooru / Gelbooru**，选中图片后把
**图片 + 提示词**一起输出给工作流。

| 项 | 值 |
| --- | --- |
| 节点类 | `DanbooruGalleryNode`（`NODE_CLASS_MAPPINGS` 里唯一节点） |
| 分类 / 显示名 | `danbooru` / 「D站画廊 (Danbooru Gallery)」 |
| 输入 | 无必填；`bypass_image` / `bypass_prompts` 为 bypass 兼容槽位；隐藏输入 `selection_data` |
| 输出 | `images`（IMAGE 列表）、`prompts`（STRING 列表），`OUTPUT_IS_LIST=(True, True)` |
| 技术栈 | Python 3.9+、aiohttp（ComfyUI 自带）、requests；前端为原生 ES Module，**无构建步骤** |
| 运行依赖 | `requests>=2.28`、`urllib3>=1.26`、`Pillow>=9.0`、`numpy>=1.24`（见 `requirements.txt`） |
| 版本 | `1.0.1`（`pyproject.toml` 与 `__init__.py` 必须一致） |
| 作者 / 许可 | PTCMode · MIT（上游 [Aaalice233/ComfyUI-Danbooru-Gallery](https://github.com/Aaalice233/ComfyUI-Danbooru-Gallery)） |

---

## 2. 目录结构

```
ComfyUI-Danbooru-Gallery-Plus/
├── __init__.py                  # 包入口：版本号、注册节点、WEB_DIRECTORY、启动横幅
├── danbooru_gallery.py          # ★ 后端主体（约 2500 行）：节点 + 全部 HTTP 路由 + 搜索/认证/翻译/收藏
├── site_adapters.py             # 站点适配器：URL、请求参数、响应归一化（Danbooru / Gelbooru）
├── site_clients.py              # HTTP 传输层：host 白名单、限速、429/503 重试、URL 脱敏
├── post_cache.py                # Gelbooru 详情持久缓存 + 本地标签分类（SQLite）
├── logger.py                    # 文件日志：单文件覆写、20MB 截断、运行期可调级别
├── settings.json                # 本地运行配置（.gitignore，不入库；含用户 API Key）
├── pyproject.toml               # 打包元数据与版本号
├── requirements.txt             # 运行时依赖
├── README.md / CHANGELOG.md / LICENSE
├── AGENT.md                     # 本文件：结构说明与开发约定
├── js/
│   ├── danbooru_gallery.js      # ★ 前端主体（约 6800 行）：节点 UI、搜索、瀑布流、设置、入队
│   └── global/
│       ├── toast_manager.js     # 全局 toast / 执行状态栏（含样式）
│       ├── multi_language.js    # 全局 i18n：namespace 注册、localStorage 记住语言
│       ├── logger_client.js     # 浏览器日志批量转发到 /danbooru_gallery_node/logs/batch
│       └── translations/danbooru_translations.js  # zh / en / ko 文案字典
├── zh_cn/                       # 中文词条数据（见 3.6）
├── cache/                       # 运行期产物：gallery_post_cache.db（.gitignore）
└── logs/                        # 运行期产物：danbooru_gallery_node.log（.gitignore）
```

**没有任何构建/打包步骤**：ComfyUI 按 `WEB_DIRECTORY = "./js"` 把 `js/` 原样挂到
`/extensions/ComfyUI-Danbooru-Gallery-Plus/`，浏览器直接加载 ES Module。

---

## 3. 后端

### 3.1 加载流程

1. ComfyUI 导入 `__init__.py` → 初始化 logger → `from .danbooru_gallery import NODE_CLASS_MAPPINGS`
   （导入时即完成所有 `@PromptServer.instance.routes.*` 路由注册）→ 打印版本横幅。
2. 启动时 `apply_log_settings()` 把 `settings.json` 里的日志开关应用到 `logger.py`。

> 所有插件路由前缀为 `/danbooru_gallery/`；只有日志批处理是
> `/danbooru_gallery_node/logs/batch`（刻意避开与原版插件冲突）。

### 3.2 节点执行链（出图）

```
前端入队前：POST /danbooru_gallery/selection_snapshot  →  后端内存快照（token → selections）
            hidden widget selection_data = {"token": "...", "queue_id": N, "selections": [...]}
节点执行：get_selected_data(selection_data)
          ├─ _parse_selection_payload()  容错解析（JSON 被截断也能用正则抠出 token）
          ├─ load_selection_snapshot()   优先用内存快照（256 条 / 6 小时 TTL）
          ├─ 逐条：缺 prompt / image_url 时 _fetch_post_by_id() 回站补齐
          ├─ _load_image_tensor() 下载 → 失败给 1×1 占位图（不中断其它张）
          └─ 返回 (images, prompts)
```

- `IS_CHANGED` 用 `token + queue_id + post_id` 判断是否重跑，避免命中 ComfyUI 执行缓存。
- 提示词由前端生成（`prompt_ready`）；后端兜底 `_build_prompt_from_post()` 按 UI 设置重建
  （类别筛选、`filter_tags`、格式转换）。

### 3.3 搜索链路（列表）

```
GET /danbooru_gallery/posts
  └─ get_posts_for_front()                    aiohttp 路由，放线程池避免堵事件循环
      └─ get_posts_internal()                 核心：缓存 → 词条上限 → 请求 → 归一化
          ├─ get_site_adapter(source)         danbooru / gelbooru
          ├─ build_site_auth(adapter)         Danbooru 的 Basic Auth（见 3.4）
          ├─ build_query_tags()               截断到账号上限，返回 (final_tags, dropped)
          ├─ 内存缓存命中？                    key 含 source/options/tags/max_tags/limit/page/rating/before_id
          ├─ adapter.build_posts_params()     拼 tags/limit/page/pid
          ├─ _danbooru_request / _gelbooru_request   限速 + 重试 + host 校验
          ├─ 422 TagLimitError？              解析真实上限 → 自动降级重试一次（并记录截断）
          └─ adapter.normalize_posts_response() → JSON 数组
```

响应头 `X-Gallery-Tag-Notice`（**仅实际截断时**）：
`{"truncated":true,"limit":3,"total":4,"dropped":["tattoo"],"dropped_count":1,"source":"danbooru"}`
→ `urllib.parse.quote` 百分号编码成 ASCII，前端 `decodeURIComponent` 还原。

### 3.4 认证与词条上限（最容易踩坑，2026-11 实测）

| 站点 | 认证方式 | 位置 |
| --- | --- | --- |
| Danbooru | HTTP Basic Auth（用户名 + API Key） | `build_site_auth()`，每个 `posts.json` 请求都要带 |
| Gelbooru | query 参数 `user_id` + `api_key` | `GelbooruAdapter.apply_auth_params()` |

- **关键坑**：旧代码用 `adapter.requires_auth` 当"是否带认证"的开关。Danbooru 匿名也能搜索，
  所以 `requires_auth=False` 让已保存的 API Key **从未被发出**，第 3 个词条必然 422。
  现在只要填了用户名 + API Key 就带认证，**不要退回旧写法**。
- 词条上限：匿名 2，登录会员（level 20）3，更高等级更多；超限返回
  `422 PostQuery::TagLimitError`，`message` 里带真实上限（`parse_danbooru_tag_limit()` 会解析）。
- 计名额：`order:` `sort:` `user:` `-tag` 等**都算**；`score:` `rating:` `status:` `id:` `date:`
  `parent:` `child:` `md5:` `is:` `filesize:` `width:` `height:` `ratio:` `mpixels:` `tagcount:` `age:`
  等不占名额（`DANBOORU_TAG_LIMIT_EXEMPT`）。
- 行为约定：
  - 未配置 Danbooru 凭据 → 上限压到 2（`DANBOORU_ANONYMOUS_TAG_LIMIT`），避免必然 422；
  - 站点上限比设置低 → 自动降级重试一次；
  - 真正丢弃了词条 → 写 `_notice` → 路由转成响应头 → 前端提示（**只提示一次/每条新搜索**）。
- Gelbooru：无凭据走公开 HTML 解析（`_fetch_gelbooru_public_posts`）；DAPI 返回 401 也回退 HTML。

### 3.5 缓存（三层，别混淆）

| 层 | 位置 | 说明 |
| --- | --- | --- |
| 列表内存缓存 | `DanbooruGalleryNode._post_cache` | 最多 200 条，TTL `max_cache_age`；**error 占位不写入** |
| Gelbooru 持久缓存 | `cache/gallery_post_cache.db` | `post_cache.py`，SQLite，存详情/标签分类，TTL `persistent_post_cache_age` |
| 前端会话状态 | 浏览器内存 + 工作流 widget | 搜索词存 hidden widget `search_query`；列表/滚动位置按「工作流 + 节点 id」存内存，切标签页恢复 |

`post_cache.py` 还会从 `../shared/data/tags_cache.db`（上游插件留下的标签库）兜底做标签分类，
文件不存在也不影响运行。

### 3.6 中文词条数据（`zh_cn/`）

| 文件 | 用途 |
| --- | --- |
| `all_tags_cn.json` | 英文 → 中文词条映射（约 400KB） |
| `danbooru.csv` | 词条中文对照表 |
| `wai_characters.csv` | 角色补充对照 |

由 `TagTranslationSystem` 加载（`load_translation_data()`），提供英↔中查询与搜索索引。

### 3.7 日志

- 文件：`logs/danbooru_gallery_node.log`，**每次启动覆写**，超过 20MB 自动清空。
- 级别优先级：环境变量 `COMFYUI_LOG_LEVEL` > 设置面板（`log_level`）> 默认 `INFO`。
- `log_console=False` 时终端只保留 ERROR。
- 访问日志统一走 `_redact_url()`，`api_key` / `login` / `token` 等 query 会被打码。

### 3.8 HTTP 路由一览

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/danbooru_gallery/posts` | 帖子列表（核心；带 `X-Gallery-Tag-Notice`） |
| GET | `/danbooru_gallery/image_proxy` | 图片代理（host 白名单 `donmai.us` / `gelbooru.com`，校验 Content-Type） |
| GET/POST | `/danbooru_gallery/user_auth` | 读取/保存 Danbooru + Gelbooru 凭据 |
| POST | `/danbooru_gallery/verify_auth` | 验证凭据 |
| GET | `/danbooru_gallery/favorites` | 收藏列表 |
| POST | `/danbooru_gallery/favorites/add`、`/remove` | 收藏增删 |
| GET | `/danbooru_gallery/check_network` | 站点连通性检查（前端 30s TTL 缓存结果） |
| GET/POST | `/danbooru_gallery/blacklist` | 黑名单 |
| GET/POST | `/danbooru_gallery/filter_tags` | 提示词过滤词 |
| GET/POST | `/danbooru_gallery/language` | 界面语言 |
| GET/POST | `/danbooru_gallery/ui_settings` | UI 设置（含 `max_search_tags`） |
| GET | `/danbooru_gallery/log_config` | 当前日志配置与文件路径 |
| POST | `/danbooru_gallery/selection_snapshot` | 保存入队快照（token） |
| GET | `/danbooru_gallery/translate_tag` | 单词条翻译 |
| POST | `/danbooru_gallery/translate_tags_batch` | 批量翻译（悬浮提示用） |
| POST | `/danbooru_gallery_node/logs/batch` | 浏览器日志批量写入后端日志 |

---

## 4. 前端

### 4.1 入口与依赖

```js
import { app } from "/scripts/app.js";
import { $el } from "/scripts/ui.js";
import { toastManagerProxy } from "./global/toast_manager.js";
import { globalMultiLanguageManager } from "./global/multi_language.js";
import { createLogger, loggerClient } from "./global/logger_client.js";

app.registerExtension({ name: "Comfy.DanbooruGallery", ... });
```

`multi_language.js` 在文件底部自行 `registerTranslations('danbooru', danbooruTranslations)`，
所以调用方只需 `t(key)`。

### 4.2 `js/danbooru_gallery.js` 的职责分区

单文件，但内部按注释分区，改代码时按函数名定位：

| 区域 | 关键符号 | 说明 |
| --- | --- | --- |
| 扩展与工作流钩子 | `app.loadGraphData`、`app.queuePrompt` | 保存/恢复会话状态、入队前后处理 |
| 会话快照 | `gallerySessionStates`、`GALLERY_SESSION_LIMIT` | 切工作流标签页后恢复搜索词、列表、滚动位置 |
| 节点 DOM | `container`、`searchInput`、`rankingButton`、`imageGrid` | 整个画廊 UI 都是手写 DOM |
| i18n | `t()`、`registerTranslations` | 所有用户可见文案走 `t()` |
| 提示 | `showTagHint()` / `clearTagHint()`、`showToast()` | 搜索框下方常驻提示 + 右上角 toast |
| 搜索/翻页 | `fetchAndRender(reset, startPageOverride)` | 核心循环，见 4.3 |
| 卡片与 tooltip | `renderPost()`、`renderErrorCell()`、`globalTooltip` | 瀑布流布局、悬浮详情、编辑面板 |
| 收藏 | `addToFavorites` / `removeFromFavorites` / `loadFavorites` | 走 3.8 的收藏接口 |
| 设置面板 | `showSettingsDialog()` | 用户认证、提示词过滤、UI、日志、预加载、`max_search_tags` |
| 入队准备 | `galleryQueuePreparers`、`/selection_snapshot` | Gelbooru 先补详情，再写快照与 hidden widget |

### 4.3 `fetchAndRender` 行为约定

- **搜索词转换**：输入框按逗号分隔 → `convertTagsToApiFormat()` 把标签内空格转 `_`、
  `\(`/`\)` 还原、特殊 `xxx:yyy` 保留原样；日期筛选追加 `date:start..end`；
  评分走 `search[rating]`（多选用 `~rating:`）。
- **分页**：Danbooru 一律走游标 `before_id`（后端翻成 `page=b<id>`）；
  Gelbooru 在 dedup 模式下也用游标，否则用 `pid` 翻页。
- **错误处理**：后端返回 error 占位时渲染错误卡；**整页都是错误占位 → `endOfResults = true`，
  停止自动补货**（否则 `maybeLoadMore` + 2s 轮询会无限重发同一失败请求）。
- **截断提示**：只有 `reset`（新搜索）时读 `X-Gallery-Tag-Notice`，拼成
  「搜索词最多 3 个（本次 4 个），已忽略：xxx」用 `showTagHint(msg, true)` 常驻显示；
  没有截断就 `clearTagHint()`。翻页/自动补货不碰它，避免重复提示。
- **去重**：`seenPostIds` 记录已渲染 id；Gelbooru dedup=off 的纯翻页模式跳过去重。
- **图片**：预览/大图都走 `/danbooru_gallery/image_proxy?url=...`，避免 Referer 防盗链。

### 4.4 设置与持久化

- 后端设置：`settings.json`（黑名单、过滤词、凭据、`max_search_tags`、日志……）。
- 前端偏好：`localStorage`（`preload_count`、`gelbooru_dedup_mode`、`global_logging`、
  `comfyui_global_language` 等）。
- 每个节点的搜索词：hidden widget `search_query`，随工作流保存。

---

## 5. `settings.json` 字段

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `language` | `"zh"` | 界面语言 |
| `blacklist` | `[]` | 屏蔽标签（整张图不显示） |
| `filter_tags` / `filter_enabled` | 示例列表 / `true` | 从输出提示词里剔除的词 |
| `danbooru_username` / `danbooru_api_key` | `""` | Danbooru 登录凭据（Basic Auth） |
| `gelbooru_user_id` / `gelbooru_api_key` | `""` | Gelbooru DAPI 凭据 |
| `gelbooru_display_all_site_content` | `false` | Gelbooru 显示全部内容 |
| `favorites` | `[]` | 收藏缓存 |
| `cache_enabled` / `max_cache_age` | `true` / `3600` | 列表内存缓存 |
| `persistent_post_cache_age` | `2592000` | Gelbooru 持久缓存 TTL（秒） |
| `default_page_size` | `20` | 历史字段，当前未使用（前端每页固定 `POSTS_PER_PAGE = 42`，预加载张数用 `localStorage.preload_count`） |
| `tooltip_enabled` | `true` | 悬浮提示 |
| `selected_categories` | `["copyright","character","general"]` | 输出哪些标签类别 |
| `multi_select_enabled` | `false` | 多选模式 |
| `source_site` | `"danbooru"` | 当前站点 |
| `log_enabled` / `log_level` / `log_console` | `true` / `"INFO"` / `false` | 日志 |
| `max_search_tags` | `2` | 单次搜索词条上限（1~20） |

---

## 6. 数据流示意

```
[浏览器] --(1) GET /danbooru_gallery/posts--> [ComfyUI/aiohttp]
   ^                                              |
   |                                              v
   |                                    get_posts_internal()
   |                                     ├─ 内存缓存命中 → 直接返回
   |                                     ├─ 词条上限/截断
   |                                     └─ requests → Danbooru/Gelbooru API
   |                                              |
   +----(2) JSON 数组 + X-Gallery-Tag-Notice -----+
```

```
选中图片 → 入队：
[前端] 选中 post → (Gelbooru 先补详情) → 生成提示词 → POST selection_snapshot
      → 写 hidden widget selection_data(token)
[后端] get_selected_data → 快照取 selections → 下载图片
      → 提示词/图片缺失时按 post_id 回站补齐 → (images, prompts)
```

```
截断提示：
后端 _notice → 路由响应头(百分号编码 JSON) → 前端 decodeURIComponent
      → 仅 reset 时 showTagHint(常驻) / clearTagHint()
```

---

## 7. 排障速查

| 现象 | 先看哪里 |
| --- | --- |
| `422 TagLimitError` | 词条数是否超过账号上限（匿名 2 / 登录 3）；`order:rank` 也占 1 个；设置里的 `max_search_tags` |
| `401 Invalid API key` | 设置 → 用户 → Danbooru 用户名/API Key；错误卡会给出这条建议 |
| 搜索一直转圈/重复请求 | 日志里是否有「本页只有错误占位」；`logs/danbooru_gallery_node.log` |
| 图片 1×1 | 看日志 `图片下载失败`；`/image_proxy` 是否被站点拦截（Cloudflare/限流） |
| Gelbooru 空列表 | 是否匿名触发限制；DAPI 401 会自动回退 HTML，看日志关键词 `改用公开网页解析` |
| 设置不生效 | `settings.json` 是否被正确写入（保存接口 `/ui_settings` 等）；日志级别是否 `WARNING` 把 INFO 吞了 |

日志文件每次启动覆写：**先复现，再立刻读**。

---

## 8. 在没有 ComfyUI 的环境里测后端

`danbooru_gallery.py` 依赖 `server`、`folder_paths`，可以 stub 掉后直接加载模块做单元/集成验证
（本仓库的修复就是这么验证的）：

```python
import importlib.util, os, sys, types

ROOT = r"path/to/ComfyUI-Danbooru-Gallery-Plus"
server = types.ModuleType("server")
class _Routes:
    def get(self, _p): return lambda f: f
    def post(self, _p): return lambda f: f
class PromptServer:
    instance = types.SimpleNamespace(routes=_Routes(), send_sync=lambda *a, **k: None)
server.PromptServer = PromptServer
sys.modules["server"] = server

fp = types.ModuleType("folder_paths")
fp.get_folder_paths = lambda *a, **k: []
fp.get_temp_directory = lambda: os.environ.get("TEMP", "/tmp")
sys.modules["folder_paths"] = fp

spec = importlib.util.spec_from_file_location(
    "dbgplus", os.path.join(ROOT, "danbooru_gallery.py"),
    submodule_search_locations=[ROOT],
)
mod = importlib.util.module_from_spec(spec)
sys.modules["dbgplus"] = mod
spec.loader.exec_module(mod)

# 直接调节点方法；路由函数可用 asyncio.run + 假 request 调用
posts_json, = mod.DanbooruGalleryNode.get_posts_internal("1girl solo", limit=2, source="danbooru")
```

注意：会真的请求站点，测试 tags 请用冷门组合，避免污染缓存与触发限流。

---

## 9. 不变量（改代码前必读）

1. **路由前缀不要改**：`/danbooru_gallery/*`；日志批处理固定 `/danbooru_gallery_node/logs/batch`。
2. **节点 key 不要改**：`DanbooruGalleryNode` 已存进用户工作流，改名会导致旧工作流失效。
3. **所有站点请求必须走** `_danbooru_request()` / `_gelbooru_request()`（限速、429/503 重试、
   host 白名单、URL 脱敏都在里面），不要裸用 `requests`。
4. **Danbooru 认证必须带 Basic Auth**：`get_posts_internal` 里用 `build_site_auth()`，
   **不要用 `adapter.requires_auth` 当开关**（会导致登录态按匿名发请求）。
5. **截断信息走 `_notice` → `X-Gallery-Tag-Notice` 头**，只有真正丢弃词条时才回；
   前端只在 `reset` 时显示，翻页不要重复提示。
6. **error 占位不要写进缓存**（否则一次限流会让 TTL 内都看不到新图）。
7. **整页错误要停自动加载**：前端置 `endOfResults = true`。
8. **不要引入构建步骤或外部 CDN**：前端是裸 ES Module，ComfyUI 直接静态托管。
9. **`settings.json` 含用户 API Key，已在 `.gitignore`**：不要提交、不要打印到日志。
10. **改版本号要同步**：`pyproject.toml` 与 `__init__.py` 的 `__version__`，并在 `CHANGELOG.md` 记一条。

---

## 10. 怎么验证 / 提交前检查

```powershell
# Python 语法
python -m py_compile danbooru_gallery.py site_adapters.py site_clients.py post_cache.py logger.py __init__.py

# 前端语法（translations 是 ESM，用 .mjs 副本校验）
node --check js/danbooru_gallery.js
Copy-Item js/global/translations/danbooru_translations.js "$env:TEMP\tr.mjs"; node --check "$env:TEMP\tr.mjs"

# 运行中的服务（端口按实际）
Invoke-WebRequest "http://127.0.0.1:8188/danbooru_gallery/posts?search%5Btags%5D=1girl+solo&limit=2&source=danbooru"
```

生效方式：**Python 改动必须重启 ComfyUI**；JS 改动浏览器 `Ctrl+F5` 强刷（ComfyUI 重启也会让浏览器重新取）。

---

## 11. 常见修改怎么做

### 11.1 新增一个站点

1. `site_adapters.py`：新增 `XxxAdapter`（`key`、`base_url`、`posts_url`、`build_posts_params`、
   `normalize_posts_response`、必要时 `apply_auth_params`），登记进 `ADAPTERS`。
2. `danbooru_gallery.py`：
   - `get_site_credentials()` / `has_required_site_credentials()` 分支；
   - `get_posts_internal()` 的请求分支（transport 用 `site_clients.py`）；
   - 前端「来源」下拉与 `source_site` 处理（`js/danbooru_gallery.js`）。
3. 别忘 `build_query_tags` 的免费限定词表（若该站点规则不同，传入自己的 `exempt_prefixes`）。

### 11.2 改 UI 文案

`js/global/translations/danbooru_translations.js` 加 `zh` / `en` key（`ko` 会继承 `en`），
代码里用 `t('key')`；带占位符的用 `.replace('{x}', ...)`。

### 11.3 加一个后端接口

在 `danbooru_gallery.py` 用 `@PromptServer.instance.routes.get/post("/danbooru_gallery/...")`，
返回 `web.json_response`；注意异常也要回 JSON（前端对非 JSON 会走「响应解析失败」兜底）。

### 11.4 调整搜索/分页

游标与页码的边界逻辑集中在 `fetchAndRender` 的 `useCursor` / `hasCursor` / `apiPage` 和
`get_posts_internal` 的 `before_id` 处理，改动时要同时照顾：
Danbooru（始终游标）、Gelbooru（dedup 才游标）、页码跳转、去重陷阱、错误占位。

---

## 12. 已知限制

- 前端单文件约 6800 行，改动靠函数名/注释定位；没有类型检查。
- 匿名访问限制严格（Danbooru 2 词、Gelbooru 公开页 42 张/页）。
- 词条上限由站点账号等级决定，插件只能截断并提示，无法突破。
- 入队快照存内存，**重启 ComfyUI 后需重新入队**（工作流里仍保留 `post_id`，可回站补齐）。
- 中文词条数据约 1MB，首次加载有一次性开销。
