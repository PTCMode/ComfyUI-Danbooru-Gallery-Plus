"""ComfyUI Danbooru Gallery Plus —— 插件入口。

在节点内浏览 Danbooru / Gelbooru，选中图片后输出「图片 + 提示词」。

- 作者：PTCMode
- 仓库：https://github.com/PTCMode/ComfyUI-Danbooru-Gallery-Plus
- 来源：抽离自 ComfyUI-Danbooru-Gallery（MIT，Aaalice233）
  https://github.com/Aaalice233/ComfyUI-Danbooru-Gallery

结构说明：
  * 只注册 DanbooruGalleryNode（D站画廊）
  * 自带前端资源（./js）、日志、站点适配器、设置与中文词条数据
  * 不依赖原插件的 py/shared、py/utils 等模块
"""

import sys

from .logger import get_logger

logger = get_logger(__name__)

# 插件版本（与 pyproject.toml 保持一致）
__version__ = "1.0.0"

# 导入节点映射（同时完成 /danbooru_gallery/* 路由注册）
from .danbooru_gallery import (  # noqa: E402
    NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS,
)

# 前端资源目录
WEB_DIRECTORY = "./js"

print("=" * 70, file=sys.stderr)
print(f"🎨 Danbooru Gallery Plus v{__version__} 加载完成 (by PTCMode)", file=sys.stderr)
for node_key, display_name in NODE_DISPLAY_NAME_MAPPINGS.items():
    print(f"   🎯 {node_key} → {display_name}", file=sys.stderr)
print("   🌐 API 前缀: /danbooru_gallery/", file=sys.stderr)
print("=" * 70, file=sys.stderr)

logger.info(
    "Danbooru Gallery Plus v%s 加载完成 (by PTCMode): %s",
    __version__, list(NODE_CLASS_MAPPINGS),
)

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY", "__version__"]
