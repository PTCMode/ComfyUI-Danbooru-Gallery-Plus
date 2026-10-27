# 更新日志

本文件记录 **ComfyUI Danbooru Gallery Plus** 的版本变化。
更早的历史来自上游项目 [ComfyUI-Danbooru-Gallery](https://github.com/Aaalice233/ComfyUI-Danbooru-Gallery)。

## v1.0.0 — 2026-10-28

首个独立版本：把画廊节点从原插件中抽离成单节点插件，并修复 / 新增以下内容。

### 新增

- 独立插件结构：只注册 `DanbooruGalleryNode`，自带前端资源、日志、站点适配器、
  中文词条数据，不再依赖原插件的 `py/shared`、`py/utils`。
- **词条跳转**：悬浮提示里的画师 / 角色 / 版权 / 通常 / meta 词条可点击，
  直达对应站点的画廊检索页；编辑面板词条菜单新增「🌐 在画廊中打开」「📖 打开词条页」。
- **设置 → 日志**：启用日志开关、日志级别（DEBUG~CRITICAL）、是否输出到 ComfyUI 终端，
  保存后立即生效（环境变量 `COMFYUI_LOG_LEVEL` 仍为最高优先级）。
- **搜索词条上限可配置**（`max_search_tags`，默认 2，范围 1~20），付费账号可调高。

### 修复

- **输出「空提示词 + 1×1 图片」**：入队数据里包含长提示词和图片 URL 时，
  部分前端扩展会把它当注释清洗（实测在 `"https:` 处被截断），后端 `json.loads`
  失败后走了空选择兜底。现在改为：入队前把完整选择 POST 到
  `/danbooru_gallery/selection_snapshot`，prompt 里只带短 token；
  解析时容错（JSON 损坏也能提取 token），并支持仅用 `post_id` 回站补齐。
- **切换会话后「选中变未选中」**：选中态改为以 `selectedEntries` 为准，
  切换工作流标签页 / 重绘 / 重新载入后自动从工作流恢复高亮；
  选中计数等状态按节点实例隔离，多个画廊节点不再互相干扰。
- **搜索词条被错误截断**：`score:>10`、`rating:`、`status:`、`id:`、`date:` 等
  实测不占 Danbooru 的「2 个搜索词」名额，现在会完整发送；
  `order:` / `sort:` / `user:` 等仍计入上限（否则站点直接 422）。
- **输入框高度被拉长**：取消自动增高，固定单行高度 + 内部滚动，布局不再随输入变化。

### 变更

- 提示词输入框改为原生 `<textarea>`，删除节点自带的自动补全
  （`AutocompleteUI`、`/autocomplete`、`/autocomplete_with_translation`、`/search_chinese`），
  补全交给 ComfyUI 生态里的全局输入框补全扩展。
- 前端日志转发接口改为 `/danbooru_gallery_node/logs/batch`，避免与原插件路由冲突。
- 搜索词改为随工作流保存在节点里，不再写入全局 `localStorage`，多标签页互不干扰。
