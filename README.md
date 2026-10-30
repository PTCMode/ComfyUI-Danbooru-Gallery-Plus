# ComfyUI Danbooru Gallery Plus

给 ComfyUI 用的 **D站图库节点**：在节点里直接浏览 Danbooru / Gelbooru，选中图片后把
**图片 + 提示词**一起输出给工作流。

> A standalone ComfyUI custom node to browse Danbooru / Gelbooru inside a node and
> output the selected images together with their prompts.
> Extracted from [ComfyUI-Danbooru-Gallery](https://github.com/Aaalice233/ComfyUI-Danbooru-Gallery),
> with several long-standing issues fixed. Author: **PTCMode**.
>
> 从 [ComfyUI-Danbooru-Gallery](https://github.com/Aaalice233/ComfyUI-Danbooru-Gallery)
> 中**单独抽离**出来的单节点插件，并修掉了几个长期存在的问题（见 [更新日志](CHANGELOG.md)）。
> 作者：**PTCMode**

![node](https://img.shields.io/badge/ComfyUI-custom%20node-7B68EE) ![license](https://img.shields.io/badge/license-MIT-green)

---

## 功能

- **节点内图库**：瀑布流浏览、无限下拉、分页跳转、评分/类别筛选、排行榜、收藏夹
- **多选输出**：选中 1 张或多张图，按选中顺序输出 `images` / `prompts`
- **标签编辑**：卡片悬浮看全部词条（带中文翻译），编辑面板可增删词条、一键重置、复制标签
- **词条跳转**：点击画师 / 角色 / 版权 / 通常词条，直接打开对应站点的画廊页或词条页
- **中文翻译**：内置 D 站词条中文对照（`zh_cn/`），支持中英双语界面
- **双站点**：Danbooru（含游标分页、详情按需拉取）与 Gelbooru（DAPI + 公开页兜底、防重复）
- **日志设置**：设置面板里可开关日志、调级别、决定是否输出到终端
- **搜索词条上限可配置**：默认 2，付费账号可调高；`score:` 等限定词不占名额
- **状态保留**：搜索词随工作流保存（重开工作流也能还原）；切换工作流标签页再切回来，已加载列表和滚动位置也会还原
- **标签补全**：本插件只把输入框做成原生 `textarea`，补全交给这类全局扩展，不自带补全


## 安装

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/PTCMode/ComfyUI-Danbooru-Gallery-Plus.git
pip install -r ComfyUI-Danbooru-Gallery-Plus/requirements.txt
```

## 节点

`danbooru` 分类 → **D站画廊 (Danbooru Gallery)**

| | 说明 |
| --- | --- |
| 输入 | 无必填项。`selection_data` 由前端自动维护（内部用）；`bypass_image` / `bypass_prompts` 是可选透传槽位，用于 bypass 时不报错 |
| 输出 | `images`（IMAGE 列表）、`prompts`（STRING 列表） |
| 用法 | 在图库里点选图片 → 执行工作流 → 按选中顺序输出对应图片与提示词 |

### 常用设置

`节点右键 → 设置`（或节点右下角齿轮）：

- **用户**：Danbooru 用户名 / API Key，Gelbooru User ID / API Key（不填也能用，但站点限流更严）
- **提示词**：输出哪些标签类别、过滤哪些词、单次搜索最多词条数
- **界面**：悬浮提示、多选模式
- **预加载**：预读张数、G 站防重复模式
- **日志**：启用日志 / 日志级别 / 是否输出到 ComfyUI 终端

## 已知限制

- 未配置 Danbooru 账号时是匿名访问，站点限制**一次最多 2 个搜索词**；
  付费账号可在 `设置 → 提示词 → 单次搜索最多词条数` 调高。

- 图片下载失败时为该张输出 1×1 占位图，并在日志里记录原因（不影响其它张）。

- 入队选择数据通过短 token + 后端内存快照传递，重启 ComfyUI 后需重新入队一次；
  工作流里仍保留 `post_id` 清单，重启后也能按 id 回站补齐。

## 常见问题

- **搜索时报 422 `PostQuery::TagLimitError`？**

  搜索词条数超了账号上限。把 `设置 → 提示词 → 单次搜索最多词条数` 调小；
`score:` `rating:` `status:` `id:` `date:` 这类限定词不占名额，但 `order:` `sort:` `user:` 会占。

- **图片一直是 1×1？**

  看 `<插件目录>/logs/danbooru_gallery_node.log`，通常是网络/代理被站点拦截或触发限流。

- **和原版 ComfyUI-Danbooru-Gallery 能共存吗？**

  不能同时启用同名节点。原版仓库里的画廊节点已停用，装本插件前请确保只有一份画廊。

## 致谢

- 上游项目：[Aaalice233/ComfyUI-Danbooru-Gallery](https://github.com/Aaalice233/ComfyUI-Danbooru-Gallery)（MIT）
- 中文词条对照数据来自上游项目仓库

## License

[MIT](LICENSE) © 2026 **PTCMode**（包含上游 Aaalice233 的 MIT 声明）

## 更新日志

见 [CHANGELOG.md](CHANGELOG.md)。
