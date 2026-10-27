"""ComfyUI Danbooru Gallery Plus —— 节点实现与 HTTP 接口。

节点：DanbooruGalleryNode（D站画廊），输出 (images, prompts) 列表。
接口：/danbooru_gallery/*（帖子列表、图片代理、设置、收藏、翻译、选择快照）。

作者：PTCMode · MIT
仓库：https://github.com/PTCMode/ComfyUI-Danbooru-Gallery-Plus
来源：抽离自 ComfyUI-Danbooru-Gallery（MIT，Aaalice233）
"""

import requests
import json
import folder_paths
from server import PromptServer
from aiohttp import web
import time
import threading
import asyncio
import torch
import io
import urllib.request
import urllib.parse
import numpy as np
from PIL import Image
import os
import csv
import re
from requests.auth import HTTPBasicAuth
import urllib3
from pathlib import Path
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import OrderedDict
from .logger import get_logger, configure_logging, get_log_config
from .site_adapters import get_site_adapter
from .site_clients import DanbooruHttpClient, GelbooruHttpClient
from .post_cache import get_gallery_post_cache
from functools import partial

logger = get_logger(__name__)

# 禁用 SSL 警告（如果需要禁用证书验证）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

REDACTED_QUERY_KEYS = {"api_key", "key", "login", "password", "token", "user_id"}

# Danbooru / Gelbooru 的限定词（metatag）形如 score:>10、order:rank、rating:g、id:<X。
# 这些不占用“免费账号最多 2 个标签”的名额。
# Danbooru / Gelbooru 的限定词（metatag）形如 score:>10、order:rank、rating:g、id:<X。
QUALIFIER_TAG_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*):")

# 实测 Danbooru 的“最多 2 个标签”判定（2026-10）：
#   不占名额：score: rating: status: id: date: parent: child: md5: is:
#             filesize: width: height: ratio: mpixels: tagcount: age:
#   会占名额：order: sort: user: pool: fav: has: source: copytags: arttags:
#             gentags: chartags: … 以及其它所有 xxx: 写法
# 所以不能简单地把所有 `xxx:yyy` 都当免费词，否则 1girl+solo+order:rank 会变成 3 个标签直接 422。
DANBOORU_TAG_LIMIT_EXEMPT = frozenset({
    "score", "rating", "status", "id", "date", "parent", "child", "md5", "is",
    "filesize", "width", "height", "ratio", "mpixels", "tagcount", "age",
})


def build_query_tags(tags, max_tags, exempt_prefixes=DANBOORU_TAG_LIMIT_EXEMPT):
    """把搜索串拆成「免费限定词 + 最多 max_tags 个计入名额的搜索词」。

    只把 `exempt_prefixes` 里的限定词（如 score:>10）视为不占名额；
    order:/sort:/user: 等仍然计入上限，避免站点返回 TagLimitError。

    Returns:
        (final_tags, dropped_tags)
    """
    date_tag = ''
    kept_tags = []
    dropped_tags = []
    tag_count = 0

    for raw_tag in str(tags or '').split(' '):
        token = raw_tag.strip()
        if not token:
            continue
        # date: 保持在末尾（沿用旧行为）
        if token.startswith('date:'):
            date_tag = token
            continue

        prefix_match = QUALIFIER_TAG_RE.match(token)
        exempt = bool(prefix_match) and prefix_match.group(1).lower() in exempt_prefixes
        if exempt or tag_count < max_tags:
            kept_tags.append(token)
            if not exempt:
                tag_count += 1
        else:
            dropped_tags.append(token)

    final_tags = ' '.join(kept_tags)
    if date_tag:
        final_tags = f"{final_tags} {date_tag}".strip()
    return final_tags, dropped_tags

HTTP_STATUS_HINTS = {
    400: ("请求参数不太对", "检查标签、页码或筛选条件是否包含站点不支持的写法。"),
    401: ("认证失败", "检查 User ID、用户名或 API Key 是否填错，必要时重新生成 API Key。"),
    403: ("访问被拒绝", "站点可能拦截了当前网络/IP、权限不足，或触发了 Cloudflare/防爬策略。"),
    404: ("资源不存在", "图片、收藏记录或接口路径可能已经不存在。"),
    408: ("请求超时", "网络太慢或站点没有及时响应，可以稍后重试。"),
    409: ("请求冲突", "资源状态可能已经改变，刷新后再试一次。"),
    422: ("站点拒绝了这次操作", "常见原因：搜索词条数超过账号上限（Danbooru 免费账号 2 个，可在节点设置 → 提示词里调低）、重复收藏、或标签参数无效。"),
    423: ("资源被锁定", "站点暂时不允许修改该资源。"),
    425: ("请求过早", "稍等几秒再试，避免站点把请求判定为异常。"),
    429: ("请求过于频繁", "已经触发限流，请降低刷新/翻页速度，等一会儿再试。"),
    500: ("站点内部错误", "对方服务器出错了，通常只能稍后再试。"),
    502: ("网关错误", "站点或中间网关暂时不稳定，可以稍后重试。"),
    503: ("服务暂不可用", "站点可能在维护、限流，或当前 IP 被临时挡住。"),
    504: ("网关超时", "站点响应太慢，可能是网络链路或服务器拥堵。"),
}


def _redact_url(raw_url):
    if not raw_url:
        return ""
    try:
        parsed = urllib.parse.urlsplit(str(raw_url))
        safe_query = []
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
            safe_query.append((key, "***" if key.lower() in REDACTED_QUERY_KEYS else value))
        return urllib.parse.urlunsplit((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(safe_query),
            parsed.fragment,
        ))
    except Exception:
        return str(raw_url)


def _response_excerpt(response, limit=500):
    if response is None:
        return ""
    try:
        text = response.text or ""
    except Exception:
        return ""
    return text[:limit]


def _classify_request_exception(exc):
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(exc, requests.exceptions.SSLError):
        return "ssl"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "connection"
    if isinstance(exc, requests.exceptions.HTTPError):
        return "http"
    return "request"


def build_gallery_error_payload(source, action, exc=None, response=None, message=None, retries_exhausted=False, retry_count=None):
    if response is None:
        response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    status_title, status_advice = HTTP_STATUS_HINTS.get(status_code, ("网络请求失败", "检查网络、代理、DNS 或稍后重试。"))
    site_name = "Gelbooru" if (source or "").lower() == "gelbooru" else "Danbooru"
    category = _classify_request_exception(exc) if exc else ("http" if status_code else "unknown")

    if category == "timeout":
        status_title = "请求超时"
        status_advice = "站点响应太慢，建议稍后重试；如果经常出现，可以检查代理或网络质量。"
    elif category == "connection":
        status_title = "连接失败"
        status_advice = "无法连到站点，可能是 DNS/代理/IP 质量、网络中断，或站点临时拦截。"
    elif category == "ssl":
        status_title = "SSL 连接失败"
        status_advice = "证书校验或 HTTPS 握手失败，常见于代理、抓包工具或系统证书异常。"

    if retries_exhausted:
        summary = f"{site_name} {action}失败：已达到最大尝试次数，最后一次结果是{status_title}。"
    else:
        summary = message or f"{site_name} {action}失败：{status_title}。"

    raw_exception = f"{type(exc).__name__}: {exc}" if exc is not None else ""
    if not raw_exception and response is not None:
        raw_exception = f"HTTP {status_code}: {getattr(response, 'reason', '')}".strip()

    details = {
        "source": site_name,
        "action": action,
        "category": category,
        "status_code": status_code,
        "status_hint": status_title,
        "retries_exhausted": bool(retries_exhausted),
        "retry_count": retry_count,
        "url": _redact_url(getattr(getattr(response, "request", None), "url", "") or getattr(response, "url", "")),
        "raw_exception": raw_exception,
        "response_excerpt": _response_excerpt(response),
    }

    return {
        "error": summary,
        "error_info": {
            "title": f"{site_name} 连接详情",
            "summary": summary,
            "causes": [
                status_advice,
                "如果只有 Gelbooru 经常失败，可能与当前 IP 信誉、地区网络或站点反爬策略有关。",
                "如果 Danbooru/Gelbooru 都失败，优先检查本机网络、代理软件、DNS 和防火墙。",
            ],
            "suggestions": [
                "稍等 30-120 秒后重试，避免连续刷新触发限流。",
                "确认代理/网络可以直接打开对应站点页面。",
                "如果使用收藏或私有内容，重新检查 User ID/API Key 是否正确。",
            ],
            "tip": "看不懂也没关系，把详情复制给 AI 问问。它很擅长把这些小刺猬一样的报错梳顺。",
            "details": details,
        },
    }

# Danbooru API文档链接 https://danbooru.donmai.us/wiki_pages/help:api

# Danbooru API的基础URL
BASE_URL = "https://danbooru.donmai.us"

_danbooru_client = DanbooruHttpClient()
_gelbooru_client = GelbooruHttpClient()

# Gelbooru 公开 HTML 页列表固定每页 42 张，pid 为该页首张图片的偏移量
GELBOORU_PUBLIC_PAGE_SIZE = 42

def _danbooru_request(method, url, **kwargs):
    """Compatibility wrapper for the isolated Danbooru transport."""
    return _danbooru_client.request(method, url, **kwargs)


def _gelbooru_request(method, url, request_kind="api", display_all=False, force_refresh=False, **kwargs):
    """Route Gelbooru traffic through its own sessions and throttles."""
    return _gelbooru_client.request(
        method,
        url,
        request_kind=request_kind,
        display_all=display_all,
        force_refresh=force_refresh,
        **kwargs,
    )


async def _run_http_request(request_func, *args, **kwargs):
    """Run a synchronous site client without blocking aiohttp's event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(request_func, *args, **kwargs))

# 获取插件目录路径
# 获取当前文件所在目录
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(PLUGIN_DIR, "settings.json")

def load_settings():
    """从本地文件加载所有设置"""
    default_settings = {
        "language": "zh",
        "blacklist": [],
        "filter_tags": [
            "watermark", "sample_watermark", "weibo_username", "weibo", "weibo_logo",
            "weibo_watermark", "censored", "mosaic_censoring", "artist_name", "twitter_username"
        ],
        "filter_enabled": True,
        "danbooru_username": "",
        "danbooru_api_key": "",
        "gelbooru_user_id": "",
        "gelbooru_api_key": "",
        "gelbooru_display_all_site_content": False,
        "favorites": [],
        "debug_mode": False,
        "cache_enabled": True,
        "max_cache_age": 3600,
        "persistent_post_cache_age": 2592000,
        "default_page_size": 20,
        "tooltip_enabled": True,
        "selected_categories": ["copyright", "character", "general"],
        "source_site": "danbooru",
        "log_enabled": True,
        "log_level": "INFO",
        "log_console": False,
        "max_search_tags": 2
    }

    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for key, value in default_settings.items():
                    if key not in data:
                        data[key] = value
                return data
    except Exception as e:
        logger.error(f"加载设置失败: {e}")

    return default_settings

def save_settings(settings):
    """保存所有设置到本地文件"""
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"保存设置失败: {e}")
        return False


def apply_log_settings(settings=None):
    """把 settings.json 里的日志开关 / 级别 / 终端输出应用到 logger。"""
    settings = settings or load_settings()
    return configure_logging(
        enabled=settings.get("log_enabled", True),
        level=settings.get("log_level", "INFO"),
        console=settings.get("log_console", False),
    )


def clamp_max_search_tags(value):
    """单次搜索词条数上限：整数 1~20，非法值退回默认 2。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 2
    return max(1, min(parsed, 20))

def load_user_auth():
    """从统一设置文件加载用户认证信息"""
    settings = load_settings()
    return settings.get("danbooru_username", ""), settings.get("danbooru_api_key", "")

def save_user_auth(username, api_key):
    """保存用户认证信息到统一设置文件"""
    settings = load_settings()
    settings["danbooru_username"] = username
    settings["danbooru_api_key"] = api_key
    return save_settings(settings)

def load_gelbooru_auth():
    """从统一设置文件加载Gelbooru API认证信息"""
    settings = load_settings()
    return settings.get("gelbooru_user_id", ""), settings.get("gelbooru_api_key", "")

def save_gelbooru_auth(user_id, api_key):
    """保存Gelbooru API认证信息到统一设置文件"""
    settings = load_settings()
    settings["gelbooru_user_id"] = user_id
    settings["gelbooru_api_key"] = api_key
    return save_settings(settings)

def get_site_credentials(adapter):
    """返回站点适配器需要的认证参数。Danbooru 使用 HTTP Basic Auth，Gelbooru 使用 query 参数。"""
    if adapter.key == "gelbooru":
        user_id, api_key = load_gelbooru_auth()
        return {"user_id": user_id, "api_key": api_key}
    return {}

def has_required_site_credentials(adapter, credentials):
    """检查当前站点适配器的必要凭据是否已配置。"""
    if adapter.key == "gelbooru":
        return bool(credentials.get("user_id") and credentials.get("api_key"))
    return True

def load_favorites():
    """从统一设置文件加载收藏列表"""
    settings = load_settings()
    return settings.get("favorites", [])

def save_favorites(favorites):
    """保存收藏列表到统一设置文件"""
    settings = load_settings()
    settings["favorites"] = favorites
    return save_settings(settings)

def load_language():
    """从统一设置文件加载语言设置"""
    settings = load_settings()
    return settings.get("language", "zh")

def save_language(language):
    """保存语言设置到统一设置文件"""
    settings = load_settings()
    settings["language"] = language
    return save_settings(settings)

def load_blacklist():
    """从统一设置文件加载黑名单"""
    settings = load_settings()
    return settings.get("blacklist", [])

def save_blacklist(blacklist_items):
    """保存黑名单到统一设置文件"""
    settings = load_settings()
    settings["blacklist"] = blacklist_items
    return save_settings(settings)

def load_filter_tags():
    """从统一设置文件加载提示词过滤设置"""
    settings = load_settings()
    return settings.get("filter_tags", []), settings.get("filter_enabled", True)

def save_filter_tags(filter_tags, enabled):
    """保存提示词过滤设置到统一设置文件"""
    settings = load_settings()
    settings["filter_tags"] = filter_tags
    settings["filter_enabled"] = enabled
    return save_settings(settings)

def load_ui_settings():
    """从统一设置文件加载UI设置"""
    settings = load_settings()
    return {
        "tooltip_enabled": settings.get("tooltip_enabled", True),
        "selected_categories": settings.get("selected_categories", ["copyright", "character", "general"]),
        "multi_select_enabled": settings.get("multi_select_enabled", False),
        "source_site": settings.get("source_site", "danbooru"),
        "gelbooru_display_all_site_content": settings.get("gelbooru_display_all_site_content", False),
        "log_enabled": settings.get("log_enabled", True),
        "log_level": settings.get("log_level", "INFO"),
        "log_console": settings.get("log_console", False),
        "max_search_tags": clamp_max_search_tags(settings.get("max_search_tags", 2))
    }

def save_ui_settings(ui_settings):
    """保存UI设置到统一设置文件"""
    settings = load_settings()
    settings["tooltip_enabled"] = ui_settings.get("tooltip_enabled", True)
    settings["selected_categories"] = ui_settings.get("selected_categories", ["copyright", "character", "general"])
    settings["multi_select_enabled"] = ui_settings.get("multi_select_enabled", False)
    settings["source_site"] = ui_settings.get("source_site", "danbooru")
    settings["gelbooru_display_all_site_content"] = ui_settings.get("gelbooru_display_all_site_content", False)
    settings["max_search_tags"] = clamp_max_search_tags(
        ui_settings.get("max_search_tags", settings.get("max_search_tags", 2))
    )
    settings["log_enabled"] = bool(ui_settings.get("log_enabled", settings.get("log_enabled", True)))
    settings["log_level"] = str(ui_settings.get("log_level", settings.get("log_level", "INFO"))).upper()
    settings["log_console"] = bool(ui_settings.get("log_console", settings.get("log_console", False)))
    saved = save_settings(settings)
    if saved:
        # 立即生效，不需要重启
        apply_log_settings(settings)
    return saved

# ================================
# Tag翻译系统
# ================================

class TagTranslationSystem:
    """Tag翻译系统，负责加载、处理和查询汉化数据"""
    
    def __init__(self):
        self.en_to_cn = {}  # 英文->中文映射
        self.cn_to_en = {}  # 中文->英文映射
        self.cn_search_index = {}  # 中文搜索索引
        self.loaded = False
        self._translation_cache = {}  # 翻译缓存
        self._search_cache = {}  # 搜索缓存
        self.max_cache_size = 1000  # 最大缓存条目数
        
    def load_translation_data(self):
        """加载所有汉化数据文件"""
        if self.loaded:
            return True
            
        try:
            zh_cn_dir = os.path.join(PLUGIN_DIR, "zh_cn")
            
            # 加载JSON格式数据
            self._load_json_data(zh_cn_dir)
            # 加载CSV格式数据
            self._load_csv_data(zh_cn_dir)
            # 加载角色CSV数据
            self._load_character_csv_data(zh_cn_dir)
            
            # 构建下划线匹配映射
            self._build_underscore_variants()
            # 构建中文搜索索引
            self._build_chinese_search_index()
            
            self.loaded = True
            return True
            
        except Exception as e:
            logger.error(f"[翻译系统] 加载失败: {e}")
            return False
    
    def _load_json_data(self, zh_cn_dir):
        """加载JSON格式的翻译数据"""
        json_file = os.path.join(zh_cn_dir, "all_tags_cn.json")
        if os.path.exists(json_file):
            try:
                with open(json_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for en_tag, cn_tag in data.items():
                        if en_tag and cn_tag:
                            self.en_to_cn[en_tag.strip()] = cn_tag.strip()
                            self.cn_to_en[cn_tag.strip()] = en_tag.strip()
            except Exception as e:
                logger.error(f"[翻译系统] JSON加载失败: {e}")
    
    def _load_csv_data(self, zh_cn_dir):
        """加载CSV格式的翻译数据"""
        csv_file = os.path.join(zh_cn_dir, "danbooru.csv")
        if os.path.exists(csv_file):
            try:
                with open(csv_file, 'r', encoding='utf-8') as f:
                    reader = csv.reader(f)
                    count = 0
                    for row in reader:
                        if len(row) >= 2 and row[0] and row[1]:
                            en_tag = row[0].strip()
                            cn_tag = row[1].strip()
                            # 如果已存在翻译，跳过（保持第一个找到的）
                            if en_tag not in self.en_to_cn:
                                self.en_to_cn[en_tag] = cn_tag
                            if cn_tag not in self.cn_to_en:
                                self.cn_to_en[cn_tag] = en_tag
                            count += 1
            except Exception as e:
                logger.error(f"[翻译系统] CSV加载失败: {e}")
    
    def _load_character_csv_data(self, zh_cn_dir):
        """加载角色CSV格式的翻译数据（格式：中文名称,英文tag）"""
        csv_file = os.path.join(zh_cn_dir, "wai_characters.csv")
        if os.path.exists(csv_file):
            try:
                with open(csv_file, 'r', encoding='utf-8') as f:
                    reader = csv.reader(f)
                    count = 0
                    for row in reader:
                        if len(row) >= 2 and row[0] and row[1]:
                            cn_tag = row[0].strip()
                            en_tag = row[1].strip()
                            # 如果已存在翻译，跳过（保持第一个找到的）
                            if en_tag not in self.en_to_cn:
                                self.en_to_cn[en_tag] = cn_tag
                            if cn_tag not in self.cn_to_en:
                                self.cn_to_en[cn_tag] = en_tag
                            count += 1
            except Exception as e:
                logger.error(f"[翻译系统] 角色CSV加载失败: {e}")
    
    def _build_underscore_variants(self):
        """构建下划线变体映射，处理有无下划线的匹配问题"""
        variants_to_add = {}
        
        for en_tag, cn_tag in list(self.en_to_cn.items()):
            # 为有下划线的tag生成无下划线版本
            if '_' in en_tag:
                no_underscore = en_tag.replace('_', '')
                if no_underscore not in self.en_to_cn:
                    variants_to_add[no_underscore] = cn_tag
            
            # 为无下划线的tag生成可能的下划线版本（基于常见模式）
            else:
                # 在数字和字母之间添加下划线 (如: 1girl -> 1_girl)
                with_underscore = re.sub(r'(\d)([a-zA-Z])', r'\1_\2', en_tag)
                if with_underscore != en_tag and with_underscore not in self.en_to_cn:
                    variants_to_add[with_underscore] = cn_tag
        
        # 添加变体到主字典
        self.en_to_cn.update(variants_to_add)
    
    def _build_chinese_search_index(self):
        """构建中文搜索索引，支持部分匹配"""
        for cn_tag in self.cn_to_en.keys():
            # 为中文tag的每个字符建立索引
            for i, char in enumerate(cn_tag):
                if char not in self.cn_search_index:
                    self.cn_search_index[char] = set()
                self.cn_search_index[char].add(cn_tag)
                
                # 也为子字符串建立索引（2-3字符的组合）
                for length in [2, 3]:
                    if i + length <= len(cn_tag):
                        substring = cn_tag[i:i + length]
                        if substring not in self.cn_search_index:
                            self.cn_search_index[substring] = set()
                        self.cn_search_index[substring].add(cn_tag)
        
        # 转换set为list以便JSON序列化
        for key in self.cn_search_index:
            self.cn_search_index[key] = list(self.cn_search_index[key])
            
    
    def translate_tag(self, en_tag):
        """翻译单个英文tag到中文"""
        if not self.loaded:
            self.load_translation_data()
        
        tag_key = en_tag.strip()
        
        # 检查缓存
        if tag_key in self._translation_cache:
            return self._translation_cache[tag_key]
        
        # 查找翻译
        translation = self.en_to_cn.get(tag_key)
        
        # 添加到缓存
        if len(self._translation_cache) < self.max_cache_size:
            self._translation_cache[tag_key] = translation
        
        return translation
    
    def translate_tags_batch(self, en_tags):
        """批量翻译英文tags"""
        if not self.loaded:
            self.load_translation_data()
        
        result = {}
        for tag in en_tags:
            translation = self.en_to_cn.get(tag.strip())
            if translation:
                result[tag] = translation
        return result
    
    def search_chinese_tags(self, query, limit=10):
        """搜索中文tag，返回匹配的中文tag及对应英文tag，支持模糊搜索"""
        if not self.loaded:
            self.load_translation_data()
        
        query = query.strip()
        if not query:
            return []
        
        # 检查搜索缓存
        cache_key = f"{query}:{limit}"
        if cache_key in self._search_cache:
            return self._search_cache[cache_key]
        
        matches = {}  # 使用字典存储匹配结果和权重
        
        # 1. 精确匹配（权重10）
        if query in self.cn_to_en:
            matches[query] = 10
        
        # 2. 前缀匹配（权重8）
        for cn_tag in self.cn_to_en.keys():
            if cn_tag.startswith(query) and cn_tag not in matches:
                matches[cn_tag] = 8
        
        # 3. 索引匹配（权重6）
        if query in self.cn_search_index:
            for cn_tag in self.cn_search_index[query]:
                if cn_tag not in matches:
                    matches[cn_tag] = 6
        
        # 4. 包含匹配（权重4）
        for cn_tag in self.cn_to_en.keys():
            if query in cn_tag and cn_tag not in matches:
                matches[cn_tag] = 4
        
        # 5. 模糊匹配（权重2）- 支持字符顺序模糊匹配
        if len(query) >= 2:
            query_chars = set(query)
            for cn_tag in self.cn_to_en.keys():
                if cn_tag not in matches:
                    tag_chars = set(cn_tag)
                    # 如果查询字符的50%以上都在tag中，认为是模糊匹配
                    if len(query_chars & tag_chars) / len(query_chars) >= 0.5:
                        matches[cn_tag] = 2
        
        # 6. 部分字符匹配（权重1）
        for char in query:
            if char in self.cn_search_index:
                for cn_tag in self.cn_search_index[char]:
                    if cn_tag not in matches:
                        matches[cn_tag] = 1
        
        # 按权重和长度排序
        sorted_matches = sorted(matches.items(), key=lambda x: (-x[1], len(x[0])))
        
        # 转换为结果格式并限制数量
        results = []
        for cn_tag, weight in sorted_matches[:limit]:
            en_tag = self.cn_to_en.get(cn_tag)
            if en_tag:
                results.append({
                    'chinese': cn_tag,
                    'english': en_tag,
                    'weight': weight
                })
        
        # 添加到缓存
        if len(self._search_cache) < self.max_cache_size:
            self._search_cache[cache_key] = results
        
        return results

# 全局翻译系统实例
translation_system = TagTranslationSystem()

# 预加载翻译数据
def preload_translation_data():
    """预加载翻译数据，在服务器启动时调用"""
    try:
        success = translation_system.load_translation_data()
        if not success:
            logger.warning("[翻译系统] 预加载失败")
    except Exception as e:
        logger.error(f"[翻译系统] 预加载异常: {e}")

# 在模块加载时预加载翻译数据
preload_translation_data()

def check_network_connection(source="danbooru"):
    """Check network connectivity for the selected gallery source."""
    adapter = get_site_adapter(source)
    try:
        # 使用一个简单的公开API端点来检测连接
        if adapter.key == "gelbooru":
            ui_settings = load_ui_settings()
            display_all = ui_settings.get("gelbooru_display_all_site_content", False)
            response = _gelbooru_request(
                "GET",
                adapter.posts_url,
                request_kind="public",
                display_all=display_all,
                params=adapter.build_public_posts_params("", 1, 1, None),
                timeout=(8, 10),
            )
            if response.status_code == 200:
                return True, False, None
            return False, True, build_gallery_error_payload(
                adapter.key,
                "network check",
                response=response,
                retries_exhausted=True,
                retry_count=1,
            )

        test_url = f"{BASE_URL}/posts.json?limit=1"
        response = _danbooru_request("GET", test_url, timeout=10)
        if response.status_code == 200:
            return True, False, None
        return False, True, build_gallery_error_payload(
            adapter.key,
            "network check",
            response=response,
            retries_exhausted=True,
            retry_count=1,
        )
    except requests.exceptions.Timeout as e:
        logger.error("网络连接超时")
        return False, True, build_gallery_error_payload(adapter.key, "network check", exc=e, retries_exhausted=True, retry_count=1)
    except requests.exceptions.RequestException as e:
        logger.error(f"网络连接失败: {e}")
        return False, True, build_gallery_error_payload(adapter.key, "network check", exc=e, retries_exhausted=True, retry_count=1)
    except Exception as e:
        logger.error(f"网络检测发生未知错误: {e}")
        return False, True, build_gallery_error_payload(adapter.key, "network check", exc=e, retries_exhausted=True, retry_count=1)

def verify_danbooru_auth(username, api_key):
    """验证Danbooru用户认证"""
    if not username or not api_key:
        return False, False
    try:
        test_url = f"{BASE_URL}/profile.json?login={username}&api_key={api_key}"
        response = _danbooru_request("GET", test_url, auth=HTTPBasicAuth(username, api_key), timeout=15)
        is_valid = response.status_code == 200
        return is_valid, False
    except Exception as e:
        logger.error(f"验证用户认证失败: {e}")
        return False, True

def get_user_favorites(username, api_key):
    """获取用户的收藏列表"""
    try:
        favorites_url = f"{BASE_URL}/favorites.json?login={username}&api_key={api_key}"
        response = _danbooru_request("GET", favorites_url, auth=HTTPBasicAuth(username, api_key), timeout=15)
        if response.status_code == 200:
            return response.json()
        return []
    except Exception as e:
        logger.error(f"获取用户收藏列表失败: {e}")
        return []

# --- 省略其他不相关的路由和函数以保持简洁 ---

def get_gelbooru_favorites(user_id, api_key, limit=1000):
    """Fetch Gelbooru favorite post IDs for the configured user."""
    if not user_id or not api_key:
        return []
    if not str(user_id).isdigit():
        raise ValueError("Gelbooru User ID must be numeric")

    adapter = get_site_adapter("gelbooru")
    credentials = {"user_id": user_id, "api_key": api_key}

    # Gelbooru's documented favorite DAPI may return an empty list for valid API
    # credentials, while the site's own favorites link uses the fav:<user_id> tag.
    favorite_posts = []
    page = 1
    per_page = min(max(int(limit), 1), 100)
    while len(favorite_posts) < limit:
        params = adapter.build_posts_params(f"fav:{user_id}", per_page, page, None)
        params = adapter.apply_auth_params(params, credentials)
        response = _gelbooru_request("GET", adapter.posts_url, request_kind="api", params=params, timeout=15)
        response.raise_for_status()
        posts = adapter.normalize_posts_response(response.json())
        if not posts:
            break
        favorite_posts.extend(str(post.get("id")) for post in posts if post.get("id"))
        if len(posts) < per_page:
            break
        page += 1

    if favorite_posts:
        return favorite_posts[:limit]

    all_ids = []
    page = 1

    while len(all_ids) < limit:
        params = adapter.build_favorites_params(user_id, per_page, page)
        params = adapter.apply_auth_params(params, credentials)
        response = _gelbooru_request("GET", adapter.posts_url, request_kind="api", params=params, timeout=15)
        if response.status_code == 401:
            raise requests.exceptions.HTTPError("Gelbooru favorite API authentication failed", response=response)
        response.raise_for_status()
        page_ids = adapter.normalize_favorites_response(response.json())
        if not page_ids:
            break
        all_ids.extend(page_ids)
        if len(page_ids) < per_page:
            break
        page += 1

    return all_ids[:limit]

def add_gelbooru_favorite(post_id, user_id, api_key):
    """Add a Gelbooru favorite via the site's favorite endpoint."""
    if not user_id or not api_key:
        return False, "请先在设置中配置 Gelbooru User ID 和 API Key"

    display_all = load_ui_settings().get("gelbooru_display_all_site_content", False)
    response = _gelbooru_request(
        "GET",
        "https://gelbooru.com/public/addfav.php",
        request_kind="api",
        display_all=display_all,
        params={"id": str(post_id), "user_id": user_id, "api_key": api_key},
        headers=_gelbooru_browser_headers("*/*"),
        timeout=(8, 15),
    )
    response.raise_for_status()
    body = (response.text or "").strip()
    if body == "1":
        return True, "已收藏，无需重复操作"
    if body == "2":
        return False, "Gelbooru 返回未登录；该站点的添加收藏接口可能不接受 API-only 认证"
    return True, "收藏成功"

def remove_gelbooru_favorite(post_id, user_id, api_key):
    """Remove a Gelbooru favorite via the site's favorite page endpoint."""
    if not user_id or not api_key:
        return False, "请先在设置中配置 Gelbooru User ID 和 API Key"

    display_all = load_ui_settings().get("gelbooru_display_all_site_content", False)
    response = _gelbooru_request(
        "GET",
        "https://gelbooru.com/index.php",
        request_kind="api",
        display_all=display_all,
        params={
            "page": "favorites",
            "s": "delete",
            "id": str(post_id),
            "user_id": user_id,
            "api_key": api_key,
        },
        headers=_gelbooru_browser_headers("text/html,*/*"),
        allow_redirects=False,
        timeout=(8, 15),
    )
    if response.status_code in (200, 302, 303):
        location = response.headers.get("Location", "")
        if "account" in location and "login" in location:
            return False, "Gelbooru 返回登录页；该站点的取消收藏接口可能不接受 API-only 认证"
        return True, "取消收藏成功"
    response.raise_for_status()
    return True, "取消收藏成功"

@PromptServer.instance.routes.post("/danbooru_gallery/favorites/add")
async def add_favorite(request):
    """添加收藏"""
    try:
        data = await request.json()
        post_id = data.get("post_id")
        source = data.get("source", "danbooru")
        adapter = get_site_adapter(source)

        if not post_id:
            return web.json_response({"success": False, "error": "缺少post_id"})

        if adapter.key == "gelbooru":
            user_id, gelbooru_api_key = load_gelbooru_auth()
            try:
                ok, message = await _run_http_request(
                    add_gelbooru_favorite,
                    post_id,
                    user_id,
                    gelbooru_api_key,
                )
                return web.json_response({"success": ok, "message": message, "error": None if ok else message})
            except requests.exceptions.Timeout as e:
                logger.error("[Gelbooru] 添加收藏请求超时")
                payload = {"success": False}
                payload.update(build_gallery_error_payload(adapter.key, "添加收藏", exc=e, retries_exhausted=True, retry_count=1))
                return web.json_response(payload)
            except requests.exceptions.RequestException as e:
                logger.error(f"[Gelbooru] 添加收藏请求失败: {type(e).__name__}")
                payload = {"success": False}
                payload.update(build_gallery_error_payload(adapter.key, "添加收藏", exc=e, retries_exhausted=True, retry_count=1))
                return web.json_response(payload)

        username, api_key = load_user_auth()
        if not username or not api_key:
            return web.json_response({"success": False, "error": "请先在设置中配置用户名和API Key"})

        # 验证认证
        is_valid, is_network_error = await _run_http_request(
            verify_danbooru_auth,
            username,
            api_key,
        )
        if is_network_error:
            return web.json_response({"success": False, "error": "网络错误，无法连接到Danbooru服务器"})
        if not is_valid:
            return web.json_response({"success": False, "error": "认证无效，请检查用户名和API Key"})

        try:
            favorite_url = f"{BASE_URL}/favorites.json?login={username}&api_key={api_key}"
            response = await _run_http_request(
                _danbooru_request,
                "POST",
                favorite_url,
                auth=HTTPBasicAuth(username, api_key),
                data={"post_id": post_id},
                timeout=15,
            )


            if response.status_code in [200, 201]:
                favorites = load_favorites()
                if str(post_id) not in favorites:
                    favorites.append(str(post_id))
                    save_favorites(favorites)
                return web.json_response({"success": True, "message": "收藏成功"})
            
            try:
                error_data = response.json()
                reason = error_data.get("reason", "未知")
                message = error_data.get("message", "没有提供具体信息")
            except (json.JSONDecodeError, ValueError):
                error_data = {}
                reason = "无法解析响应"
                message = response.text

            if response.status_code == 422 and "You have already favorited this post" in message:
                favorites = load_favorites()
                if str(post_id) not in favorites:
                    favorites.append(str(post_id))
                    save_favorites(favorites)
                return web.json_response({"success": True, "message": "已收藏，无需重复操作"})
                
            error_map = {
                401: "认证失败，请检查用户名和API Key",
                403: "权限不足，可能需要Gold账户或更高权限",
                404: "图片不存在",
                429: "请求过于频繁，请稍后重试 (Rate Limited)",
            }
            
            error_message = error_map.get(response.status_code, f"收藏失败，状态码: {response.status_code}, 原因: {message}")
            logger.error(error_message)
            return web.json_response({"success": False, "error": error_message})

        except requests.exceptions.Timeout as e:
            logger.error("添加收藏时网络请求超时")
            payload = {"success": False}
            payload.update(build_gallery_error_payload(adapter.key, "添加收藏", exc=e, retries_exhausted=True, retry_count=1))
            return web.json_response(payload)
        except requests.exceptions.RequestException as e:
            logger.error(f"添加收藏时网络请求失败: {e}")
            payload = {"success": False}
            payload.update(build_gallery_error_payload(adapter.key, "添加收藏", exc=e, retries_exhausted=True, retry_count=1))
            return web.json_response(payload)
        except Exception as e:
            import traceback
            logger.error(f"添加收藏时发生严重错误: {e}")
            logger.error(traceback.format_exc())
            return web.json_response({"success": False, "error": f"服务器内部错误: {e}"}, status=500)

    except Exception as e:
        logger.error(f"添加收藏接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

@PromptServer.instance.routes.post("/danbooru_gallery/favorites/remove")
async def remove_favorite(request):
    """移除收藏"""
    try:
        data = await request.json()
        post_id = data.get("post_id")
        source = data.get("source", "danbooru")
        adapter = get_site_adapter(source)

        if not post_id:
            return web.json_response({"success": False, "error": "缺少post_id"})

        if adapter.key == "gelbooru":
            user_id, gelbooru_api_key = load_gelbooru_auth()
            try:
                ok, message = await _run_http_request(
                    remove_gelbooru_favorite,
                    post_id,
                    user_id,
                    gelbooru_api_key,
                )
                return web.json_response({"success": ok, "message": message, "error": None if ok else message})
            except requests.exceptions.Timeout as e:
                logger.error("[Gelbooru] 取消收藏请求超时")
                payload = {"success": False}
                payload.update(build_gallery_error_payload(adapter.key, "取消收藏", exc=e, retries_exhausted=True, retry_count=1))
                return web.json_response(payload)
            except requests.exceptions.RequestException as e:
                logger.error(f"[Gelbooru] 取消收藏请求失败: {type(e).__name__}")
                payload = {"success": False}
                payload.update(build_gallery_error_payload(adapter.key, "取消收藏", exc=e, retries_exhausted=True, retry_count=1))
                return web.json_response(payload)
        
        username, api_key = load_user_auth()
        if not username or not api_key:
            return web.json_response({"success": False, "error": "请先在设置中配置用户名和API Key"})

        # 验证认证
        is_valid, is_network_error = await _run_http_request(
            verify_danbooru_auth,
            username,
            api_key,
        )
        if is_network_error:
            return web.json_response({"success": False, "error": "网络错误，无法连接到Danbooru服务器"})
        if not is_valid:
            return web.json_response({"success": False, "error": "认证无效，请检查用户名和API Key"})
        
        try:
            # 直接使用帖子ID删除收藏
            delete_url = f"{BASE_URL}/favorites/{post_id}.json?login={username}&api_key={api_key}"
            delete_response = await _run_http_request(
                _danbooru_request,
                "DELETE",
                delete_url,
                auth=HTTPBasicAuth(username, api_key),
                timeout=15,
            )


            if delete_response.status_code in [200, 204]:
                favorites = load_favorites()
                if str(post_id) in favorites:
                    favorites.remove(str(post_id))
                    save_favorites(favorites)
                return web.json_response({"success": True, "message": "取消收藏成功"})
            elif delete_response.status_code == 404:
                # 如果收藏不存在，视为已删除
                favorites = load_favorites()
                if str(post_id) in favorites:
                    favorites.remove(str(post_id))
                    save_favorites(favorites)
                return web.json_response({"success": True, "message": "该图片未在云端收藏，本地已同步"})

            # 如果有收藏记录但删除失败，解析错误
            try:
                error_data = delete_response.json()
                reason = error_data.get("reason", "未知")
                message = error_data.get("message", "没有提供具体信息")
            except (json.JSONDecodeError, ValueError):
                error_data = {}
                reason = "无法解析响应"
                message = delete_response.text

            error_map = {
                401: "认证失败，请检查用户名和API Key",
                403: "权限不足，可能需要Gold账户",
                404: "收藏记录不存在",
                429: "请求过于频繁，请稍后重试 (Rate Limited)",
            }

            error_message = error_map.get(delete_response.status_code, f"取消收藏失败，状态码: {delete_response.status_code}, 原因: {message}")
            logger.error(error_message)
            return web.json_response({"success": False, "error": error_message})

        except requests.exceptions.Timeout as e:
            logger.error("移除收藏时网络请求超时")
            payload = {"success": False}
            payload.update(build_gallery_error_payload(adapter.key, "取消收藏", exc=e, retries_exhausted=True, retry_count=1))
            return web.json_response(payload)
        except requests.exceptions.RequestException as e:
            logger.error(f"移除收藏时网络请求失败: {e}")
            payload = {"success": False}
            payload.update(build_gallery_error_payload(adapter.key, "取消收藏", exc=e, retries_exhausted=True, retry_count=1))
            return web.json_response(payload)
        except Exception as e:
            import traceback
            logger.error(f"移除收藏时发生严重错误: {e}")
            logger.error(traceback.format_exc())
            return web.json_response({"success": False, "error": f"服务器内部错误: {e}"}, status=500)

    except Exception as e:
        logger.error(f"移除收藏接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

@PromptServer.instance.routes.get("/danbooru_gallery/user_auth")
async def get_user_auth_route(request):
    """获取用户认证信息"""
    try:
        username, api_key = load_user_auth()
        gelbooru_user_id, gelbooru_api_key = load_gelbooru_auth()
        has_auth = bool(username and api_key)
        return web.json_response({
            "success": True,
            "username": username,
            "api_key": api_key,
            "has_auth": has_auth,
            "gelbooru_user_id": gelbooru_user_id,
            "gelbooru_api_key": gelbooru_api_key,
            "gelbooru_has_auth": bool(gelbooru_user_id and gelbooru_api_key)
        })
    except Exception as e:
        logger.error(f"获取用户认证接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

@PromptServer.instance.routes.get("/danbooru_gallery/favorites")
async def get_favorites_route(request):
    """获取收藏列表"""
    try:
        source = request.query.get("source") or load_ui_settings().get("source_site", "danbooru")
        adapter = get_site_adapter(source)
        if adapter.key == "gelbooru":
            user_id, gelbooru_api_key = load_gelbooru_auth()
            if not user_id or not gelbooru_api_key:
                return web.json_response({
                    "success": False,
                    "favorites": [],
                    "source": adapter.key,
                    "error": "请先在设置中配置 Gelbooru User ID 和 API Key"
                })
            try:
                favorites = await _run_http_request(
                    get_gelbooru_favorites,
                    user_id,
                    gelbooru_api_key,
                )
                return web.json_response({"success": True, "favorites": favorites, "source": adapter.key})
            except requests.exceptions.Timeout as e:
                logger.error("[Gelbooru] 获取收藏列表超时")
                payload = {
                    "success": False,
                    "favorites": [],
                    "source": adapter.key,
                }
                payload.update(build_gallery_error_payload(adapter.key, "读取收藏列表", exc=e, retries_exhausted=True, retry_count=1))
                return web.json_response(payload)
            except requests.exceptions.HTTPError as e:
                status_code = e.response.status_code if e.response is not None else "unknown"
                logger.error(f"[Gelbooru] 获取收藏列表失败: HTTP {status_code}")
                payload = {
                    "success": False,
                    "favorites": [],
                    "source": adapter.key,
                }
                payload.update(build_gallery_error_payload(adapter.key, "读取收藏列表", exc=e, retries_exhausted=True, retry_count=1))
                return web.json_response(payload)
            except ValueError as e:
                logger.error(f"[Gelbooru] 收藏列表配置无效: {e}")
                return web.json_response({
                    "success": False,
                    "favorites": [],
                    "source": adapter.key,
                    "error": "Gelbooru User ID 必须是数字 ID，不是用户名"
                })
            except requests.exceptions.RequestException as e:
                logger.error(f"[Gelbooru] 获取收藏列表请求失败: {type(e).__name__}")
                payload = {
                    "success": False,
                    "favorites": [],
                    "source": adapter.key,
                }
                payload.update(build_gallery_error_payload(adapter.key, "读取收藏列表", exc=e, retries_exhausted=True, retry_count=1))
                return web.json_response(payload)

        favorites = load_favorites()
        return web.json_response({"success": True, "favorites": favorites, "source": adapter.key})
    except Exception as e:
        logger.error(f"获取收藏列表接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

@PromptServer.instance.routes.post("/danbooru_gallery/user_auth")
async def save_user_auth_route(request):
    """保存用户认证信息"""
    try:
        data = await request.json()
        username = data.get("username", "")
        api_key = data.get("api_key", "")
        gelbooru_user_id = data.get("gelbooru_user_id", "")
        gelbooru_api_key = data.get("gelbooru_api_key", "")
        if save_user_auth(username, api_key) and save_gelbooru_auth(gelbooru_user_id, gelbooru_api_key):
            return web.json_response({"success": True})
        else:
            return web.json_response({"success": False, "error": "无法保存用户认证信息"}, status=500)
    except Exception as e:
        logger.error(f"保存用户认证接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

@PromptServer.instance.routes.get("/danbooru_gallery/check_network")
async def check_network(request):
    """检测网络连接状态"""
    try:
        source = request.query.get("source") or load_ui_settings().get("source_site", "danbooru")
        adapter = get_site_adapter(source)
        is_connected, is_network_error, error_payload = await _run_http_request(
            check_network_connection,
            adapter.key,
        )
        payload = {
            "success": True,
            "connected": is_connected,
            "network_error": is_network_error,
            "source": adapter.key
        }
        if error_payload:
            payload.update(error_payload)
        return web.json_response(payload)
    except Exception as e:
        logger.error(f"网络检测接口错误: {e}")
        payload = {"success": False, "network_error": True}
        payload.update(build_gallery_error_payload("danbooru", "network check", exc=e))
        return web.json_response(payload, status=500)

@PromptServer.instance.routes.post("/danbooru_gallery/verify_auth")
async def verify_auth(request):
    """验证用户认证"""
    try:
        data = await request.json()
        username = data.get("username", "")
        api_key = data.get("api_key", "")

        if not username or not api_key:
            return web.json_response({"success": False, "error": "缺少用户名或API Key"})

        is_valid, is_network_error = await _run_http_request(
            verify_danbooru_auth,
            username,
            api_key,
        )
        return web.json_response({"success": True, "valid": is_valid, "network_error": is_network_error})
    except Exception as e:
        logger.error(f"验证认证接口错误: {e}")
        return web.json_response({"success": False, "error": "网络错误", "network_error": True}, status=500)

# 图片代理并发上限：浏览器一次打开一页会 lazy-load 多张缩略图，没有上限会导致
# 后端同时发出十几个 CDN 请求，配合全局限流会形成长队列；限到 3 并发即可让缩略图
# 平滑流入，又避免瞬时流量把 CF 的 rate rule 触发。
_image_proxy_semaphore = None

def _get_image_proxy_semaphore():
    global _image_proxy_semaphore
    if _image_proxy_semaphore is None:
        _image_proxy_semaphore = asyncio.Semaphore(2)
    return _image_proxy_semaphore

def _gelbooru_browser_headers(accept="text/html,*/*"):
    return _gelbooru_client.browser_headers(accept)

def _gelbooru_public_page_requires_options(html_text):
    """Detect Gelbooru's anonymous page that says results are hidden by site options."""
    if not html_text:
        return False
    return (
        "Check your blacklist" in html_text
        and "Nobody here but us" in html_text
    )

def _image_proxy_headers_for_host(host):
    """Return browser-like headers for image CDNs that are sensitive to generic requests."""
    if host == "gelbooru.com" or host.endswith(".gelbooru.com"):
        return _gelbooru_browser_headers("image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8")
    return {}

def _fetch_supported_media(url):
    """Fetch image/video bytes using the same site-aware request path as the preview proxy."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("invalid media url scheme")

    host = (parsed.hostname or "").lower()
    allowed_suffixes = ("donmai.us", "gelbooru.com")
    if not any(host == suffix or host.endswith(f".{suffix}") for suffix in allowed_suffixes):
        raise ValueError(f"unsupported media host: {host}")

    if host == "gelbooru.com" or host.endswith(".gelbooru.com"):
        display_all_site_content = load_ui_settings().get("gelbooru_display_all_site_content", False)
        response = _gelbooru_request(
            "GET",
            url,
            request_kind="image",
            display_all=display_all_site_content,
            headers=_image_proxy_headers_for_host(host),
            timeout=(8, 30),
        )
    else:
        response = _danbooru_request(
            "GET",
            url,
            headers=_image_proxy_headers_for_host(host),
            timeout=30,
        )

    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "application/octet-stream").lower()
    if content_type and not content_type.startswith(("image/", "application/octet-stream")):
        raise ValueError(f"upstream returned non-image content: {content_type}")
    return response.content

def _fetch_gelbooru_public_posts(
    adapter,
    tags,
    limit,
    page,
    rating_query,
    display_all_site_content=False,
    hydrate_details=False,
):
    """Fetch Gelbooru posts from public HTML pages when DAPI credentials are unavailable.
    Uses fixed page size GELBOORU_PUBLIC_PAGE_SIZE (42) so pid is decoupled from
    the frontend's limit.  Frontend chains multiple fetches via maybeLoadMore.
    On 429/503 retries once with Retry-After.  Returns error placeholders on failure.
    """
    id_match = re.search(r"(?:^|\s)id:(\d+)(?:\s|$)", tags or "")
    if id_match:
        refs = [{"id": id_match.group(1)}]
    else:
        def _get_list_page(_params, _pid, force_refresh=False):
            """Fetch a list page through the isolated Gelbooru transport."""
            try:
                response = _gelbooru_request(
                    "GET",
                    adapter.posts_url,
                    request_kind="public",
                    display_all=display_all_site_content,
                    force_refresh=force_refresh,
                    params=_params,
                    timeout=(8, 15),
                )
                if response.status_code in (429, 503):
                    _log_gelbooru_retry_warning(f"重试仍限流({response.status_code})，跳过本页", pid_value=_pid)
                    return None
                return response
            except requests.exceptions.RequestException as exc:
                _log_gelbooru_retry_warning(f"网络异常: {exc}", pid_value=_pid)
                return None

        # 一次请求多页：limit > 42 时依次抓取多页再合并，顺序请求不跨序
        pages_to_fetch = max(1, -(-limit // GELBOORU_PUBLIC_PAGE_SIZE))
        all_refs = []
        for pi in range(pages_to_fetch):
            cur_page = page + pi
            pid_value = max(cur_page - 1, 0) * GELBOORU_PUBLIC_PAGE_SIZE
            params = adapter.build_public_posts_params(tags, limit, cur_page, rating_query)
            params["pid"] = pid_value

            list_response = _get_list_page(params, pid_value)
            if list_response is None:
                payload = build_gallery_error_payload(
                    adapter.key,
                    "公开页抓取",
                    message="Gelbooru 公开页抓取失败：已达到最大尝试次数，可能是限流、站点拦截或当前 IP 质量不佳。",
                    retries_exhausted=True,
                    retry_count=2,
                )
                all_refs.append({**payload, "pid": pid_value, "page": cur_page})
                continue

            if display_all_site_content and _gelbooru_public_page_requires_options(list_response.text):
                logger.warning("[GelbooruPublic] Display all site content needed; rebuilding session")
                list_response = _get_list_page(params, pid_value, force_refresh=True)
                if list_response is None:
                    payload = build_gallery_error_payload(
                        adapter.key,
                        "公开页抓取",
                        message="Gelbooru 公开页抓取失败：已达到最大尝试次数，可能是限流、站点拦截或当前 IP 质量不佳。",
                        retries_exhausted=True,
                        retry_count=2,
                    )
                    all_refs.append({**payload, "pid": pid_value, "page": cur_page})
                    continue

            remaining = limit - len(all_refs)
            refs = adapter.extract_public_post_refs(list_response.text, remaining)
            all_refs.extend(refs)
            logger.info(f"[GelbooruPublic] page={cur_page} pid={pid_value} refs={len(refs)} 累计={len(all_refs)}")

            if len(refs) < GELBOORU_PUBLIC_PAGE_SIZE:
                break

        refs = all_refs
        logger.info(f"[GelbooruPublic] tags='{tags}' total_refs={len(refs)}")

    # Optional compatibility path for callers that explicitly request eager
    # categorized details. Normal gallery loads use local classification and
    # bounded viewport prefetch instead.
    # The list HTML already carries the complete, uncategorized tag string in
    # each thumbnail title. Avoid blocking the first render on up to 42
    # rate-limited detail requests; categorized details are hydrated on demand.
    if hydrate_details and not id_match and refs:
        def _hydrate_one(ref):
            """Fetch and hydrate a single post's detail page."""
            try:
                resp = _gelbooru_request(
                    "GET",
                    adapter.posts_url,
                    request_kind="hydrate",
                    display_all=display_all_site_content,
                    params=adapter.build_public_post_params(ref["id"]),
                    timeout=(8, 15),
                )
                resp.raise_for_status()
                hydrated = adapter.normalize_public_post_page(ref["id"], resp.text, ref)
                for key in ("tag_string", "tag_string_artist", "tag_string_copyright", "tag_string_character", "tag_string_general", "tag_string_meta", "image_width", "image_height", "rating"):
                    ref[key] = hydrated.get(key, ref.get(key, ""))
                ref["preview_file_url"] = hydrated.get("preview_file_url") or ref.get("preview_file_url", "")
                if hydrated.get("file_url"):
                    ref["file_url"] = hydrated["file_url"]
                    ref["_gelbooru_preview_only"] = False
            except Exception:
                pass
            return ref

        with ThreadPoolExecutor(max_workers=2) as executor:
            for i in range(0, len(refs), 2):
                pair = refs[i:i+2]
                futures = [executor.submit(_hydrate_one, ref) for ref in pair]
                for f in futures:
                    f.result()  # 按序等待两张完成

    if not id_match:
        return refs

    # id_match 模式：逐帖 hydrate
    posts = []
    for ref in refs:
        post_id = ref.get("id")
        if not post_id:
            continue

        try:
            post_response = _gelbooru_request(
                "GET",
                adapter.posts_url,
                request_kind="hydrate",
                display_all=display_all_site_content,
                params=adapter.build_public_post_params(post_id),
                timeout=(8, 15),
            )
            post_response.raise_for_status()
            post = adapter.normalize_public_post_page(post_id, post_response.text, ref)
            has_original = bool(post.get("file_url"))
            exact_tags = bool(post.get("_tag_categories_exact"))
            preview_only = bool(post.get("_gelbooru_preview_only"))
            tag_count = len(str(post.get("tag_string") or "").split())
            if not has_original or not exact_tags or preview_only:
                logger.warning(
                    "[GelbooruPublic] 详情不满足入队要求 id=%s status=%s "
                    "content_type=%s bytes=%s missing_original=%s "
                    "missing_exact_tags=%s preview_only=%s tag_count=%s",
                    post_id,
                    post_response.status_code,
                    post_response.headers.get("Content-Type", "unknown"),
                    len(post_response.content),
                    not has_original,
                    not exact_tags,
                    preview_only,
                    tag_count,
                )
            if post.get("preview_file_url") or post.get("file_url"):
                posts.append(post)
        except requests.exceptions.RequestException as e:
            status_code = e.response.status_code if e.response is not None else "none"
            logger.warning(
                "[GelbooruPublic] 详情请求失败 id=%s display_all=%s status=%s "
                "error=%s: %s",
                post_id,
                bool(display_all_site_content),
                status_code,
                type(e).__name__,
                e,
            )
        except Exception:
            logger.exception("[GelbooruPublic] 详情解析异常 id=%s", post_id)
            raise

    return posts


def _log_gelbooru_retry_warning(message, pid_value=None):
    """Short helper for logging gelbooru public fetch retry messages."""
    if pid_value is not None:
        logger.warning(f"[GelbooruPublic] {message} (pid={pid_value})")
    else:
        logger.warning(f"[GelbooruPublic] {message}")

@PromptServer.instance.routes.get("/danbooru_gallery/image_proxy")
async def image_proxy(request):
    # 浏览器直连 cdn.donmai.us 会被 Cloudflare 按 cross-site <img> 请求挑战并返回 403，
    # 而 Danbooru 客户端使用描述性 UA 能过 CF。转发一次即可让前端拿到缩略图。
    url = request.query.get("url", "")
    if not url:
        return web.Response(status=400, text="missing url")

    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return web.Response(status=400, text="invalid url")

    if parsed.scheme not in ("http", "https"):
        return web.Response(status=400, text="invalid scheme")

    # SSRF 防护：只允许当前支持图站的图片域名
    host = (parsed.hostname or "").lower()
    allowed_suffixes = ("donmai.us", "gelbooru.com")
    if not any(host == suffix or host.endswith(f".{suffix}") for suffix in allowed_suffixes):
        return web.Response(status=403, text="host not allowed")

    async with _get_image_proxy_semaphore():
        try:
            if host == "gelbooru.com" or host.endswith(".gelbooru.com"):
                display_all_site_content = load_ui_settings().get("gelbooru_display_all_site_content", False)
                resp = await asyncio.to_thread(
                    _gelbooru_request,
                    "GET",
                    url,
                    request_kind="image",
                    display_all=display_all_site_content,
                    headers=_image_proxy_headers_for_host(host),
                    timeout=(8, 15),
                )
            else:
                resp = await asyncio.to_thread(
                    _danbooru_request,
                    "GET",
                    url,
                    headers=_image_proxy_headers_for_host(host),
                    timeout=15,
                )
        except requests.exceptions.RequestException as e:
            logger.warning(f"[ImageProxy] 上游请求失败 {url}: {e}")
            return web.Response(status=502, text="upstream error")

    if resp.status_code != 200:
        logger.debug(f"[ImageProxy] 上游返回 {resp.status_code}: {url}")
        return web.Response(status=resp.status_code)

    content_type = resp.headers.get("Content-Type", "application/octet-stream")
    if not content_type.lower().startswith(("image/", "video/")):
        logger.warning(f"[ImageProxy] 上游返回非媒体内容 {content_type}: {url}")
        return web.Response(status=502, text="upstream returned non-media content")

    return web.Response(
        body=resp.content,
        headers={
            "Content-Type": content_type,
            "Cache-Control": "public, max-age=86400",
        },
    )

# --- 保留文件中剩余的其他部分 ---
@PromptServer.instance.routes.get("/danbooru_gallery/posts")
async def get_posts_for_front(request):
    query = request.query
    tags = query.get("search[tags]", "")
    page = query.get("page", "1")
    limit = query.get("limit", "100")
    rating = query.get("search[rating]", "")
    source = query.get("source", "danbooru")
    gelbooru_display_all_site_content = query.get("gelbooru_display_all_site_content", "").lower() in ("1", "true", "yes", "on")
    force_public_detail = query.get("force_public_detail", "").lower() in ("1", "true", "yes", "on")
    before_id_raw = query.get("before_id", "")
    gelbooru_dedup_mode = query.get("gelbooru_dedup_mode", "off").strip().lower()
    if gelbooru_dedup_mode not in ("off", "on", "on_auth"):
        gelbooru_dedup_mode = "off"

    # Network and HTML parsing are synchronous. Keep them off aiohttp's event
    # loop so settings/favorites calls and a newly selected source stay
    # responsive while an older upstream request is still finishing.
    loop = asyncio.get_running_loop()
    posts_json_str, = await loop.run_in_executor(
        None,
        partial(
            DanbooruGalleryNode.get_posts_internal,
            tags=tags,
            limit=int(limit),
            page=int(page),
            rating=rating,
            source=source,
            gelbooru_display_all_site_content=gelbooru_display_all_site_content,
            force_public_detail=force_public_detail,
            before_id=before_id_raw,
            gelbooru_dedup_mode=gelbooru_dedup_mode,
        ),
    )
    
    try:
        posts_list = json.loads(posts_json_str)
    except json.JSONDecodeError:
        posts_list = []

    return web.json_response(posts_list, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0"
    })

@PromptServer.instance.routes.get("/danbooru_gallery/log_config")
async def get_log_config_route(request):
    """返回当前生效的日志配置（设置面板用来显示日志文件位置）。"""
    try:
        config = get_log_config()
        config["settings"] = {
            "log_enabled": load_settings().get("log_enabled", True),
            "log_level": load_settings().get("log_level", "INFO"),
            "log_console": load_settings().get("log_console", False),
        }
        return web.json_response({"success": True, "config": config})
    except Exception as e:
        logger.error(f"[LogConfig] 读取日志配置失败: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

@PromptServer.instance.routes.get("/danbooru_gallery/blacklist")
async def get_blacklist(request):
    blacklist = load_blacklist()
    return web.json_response({"blacklist": blacklist})

@PromptServer.instance.routes.post("/danbooru_gallery/blacklist")
async def save_blacklist_route(request):
    try:
        data = await request.json()
        blacklist_items = data.get("blacklist", [])
        success = save_blacklist(blacklist_items)
        return web.json_response({"success": success})
    except Exception as e:
        logger.error(f"保存黑名单接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)})

@PromptServer.instance.routes.get("/danbooru_gallery/language")
async def get_language(request):
    language = load_language()
    return web.json_response({"language": language})

@PromptServer.instance.routes.post("/danbooru_gallery/language")
async def save_language_route(request):
    try:
        data = await request.json()
        language = data.get("language", "zh")
        success = save_language(language)
        return web.json_response({"success": success})
    except Exception as e:
        logger.error(f"保存语言设置接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)})

@PromptServer.instance.routes.get("/danbooru_gallery/filter_tags")
async def get_filter_tags(request):
    filter_tags, filter_enabled = load_filter_tags()
    return web.json_response({"filter_tags": filter_tags, "filter_enabled": filter_enabled})

@PromptServer.instance.routes.post("/danbooru_gallery/filter_tags")
async def save_filter_tags_route(request):
    try:
        data = await request.json()
        filter_tags = data.get("filter_tags", [])
        filter_enabled = data.get("filter_enabled", False)
        success = save_filter_tags(filter_tags, filter_enabled)
        return web.json_response({"success": success})
    except Exception as e:
        logger.error(f"保存提示词过滤设置接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)})

@PromptServer.instance.routes.get("/danbooru_gallery/ui_settings")
async def get_ui_settings(request):
    try:
        ui_settings = load_ui_settings()
        return web.json_response({
            "success": True,
            "settings": ui_settings
        })
    except Exception as e:
        logger.error(f"[UI_SETTINGS] 获取UI设置接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)})

@PromptServer.instance.routes.post("/danbooru_gallery/ui_settings")
async def save_ui_settings_route(request):
    try:
        data = await request.json()
        ui_settings = {
            "tooltip_enabled": data.get("tooltip_enabled", True),
            "selected_categories": data.get("selected_categories", ["copyright", "character", "general"]),
            "multi_select_enabled": data.get("multi_select_enabled", False),
            "source_site": data.get("source_site", "danbooru"),
            "gelbooru_display_all_site_content": data.get("gelbooru_display_all_site_content", False),
            "log_enabled": data.get("log_enabled", True),
            "log_level": data.get("log_level", "INFO"),
            "log_console": data.get("log_console", False),
            "max_search_tags": data.get("max_search_tags", 2)
        }
        success = save_ui_settings(ui_settings)
        return web.json_response({"success": success, "log": get_log_config()})
    except Exception as e:
        logger.error(f"保存UI设置接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)})

# ================================
# 选择快照（前端在入队前 POST 到这里）
#
# 为什么不把完整选择数据直接塞进 prompt：
#   1. 节点里被手动添加到 prompt 的长字符串会被部分前端扩展清洗/截断
#      （实测含 "//" 的图片 URL 会在 "https:" 处被截掉，JSON 变成半截），
#      节点于是只能输出空提示词 + 1x1 占位图；
#   2. 整段提示词和图片 URL 也会把工作流/prompt 撑大。
# 现在 prompt 里只留一个短 token（外加用于恢复选中态的 post_id 列表），
# 完整数据（提示词 + 图片地址）由本接口保存在内存里，节点执行时按 token 取回。
# ================================

_SELECTION_SNAPSHOT_LIMIT = 256
_SELECTION_SNAPSHOT_TTL = 6 * 3600
_selection_snapshots = OrderedDict()
_selection_snapshots_lock = threading.Lock()


def store_selection_snapshot(token, selections, node_id="", instance_id=""):
    """保存一次入队快照，返回条目数。"""
    token = str(token or "").strip()
    if not token or not isinstance(selections, list):
        return 0
    with _selection_snapshots_lock:
        _selection_snapshots[token] = {
            "selections": selections,
            "node_id": str(node_id or ""),
            "instance_id": str(instance_id or ""),
            "timestamp": time.time(),
        }
        _selection_snapshots.move_to_end(token)
        now = time.time()
        while _selection_snapshots:
            oldest_token = next(iter(_selection_snapshots))
            oldest = _selection_snapshots[oldest_token]
            expired = now - float(oldest.get("timestamp") or now) > _SELECTION_SNAPSHOT_TTL
            if len(_selection_snapshots) <= _SELECTION_SNAPSHOT_LIMIT and not expired:
                break
            _selection_snapshots.pop(oldest_token, None)
    return len(selections)


def load_selection_snapshot(token):
    """按 token 取回快照（不删除，便于同一 token 重复执行 / 批处理）。"""
    token = str(token or "").strip()
    if not token:
        return None
    with _selection_snapshots_lock:
        entry = _selection_snapshots.get(token)
        if entry is None:
            return None
        if time.time() - float(entry.get("timestamp") or 0) > _SELECTION_SNAPSHOT_TTL:
            _selection_snapshots.pop(token, None)
            return None
        return entry


@PromptServer.instance.routes.post("/danbooru_gallery/selection_snapshot")
async def save_selection_snapshot(request):
    """前端入队前把完整选择（提示词 + 图片地址）推到这里。"""
    try:
        data = await request.json()
    except Exception as e:
        return web.json_response({"success": False, "error": f"无效的 JSON: {e}"}, status=400)

    token = str(data.get("token") or "").strip()
    selections = data.get("selections")
    if not token or not isinstance(selections, list):
        return web.json_response({"success": False, "error": "缺少 token 或 selections"}, status=400)

    count = store_selection_snapshot(
        token,
        selections,
        node_id=data.get("node_id", ""),
        instance_id=data.get("instance_id", ""),
    )
    logger.info(f"[SelectionSnapshot] 已保存 {count} 条选择 token={token} node={data.get('node_id', '?')}")
    return web.json_response({"success": True, "token": token, "count": count})

# ================================
# JS 日志转发：前端 logger_client 通过 POST /danbooru_gallery_node/logs/batch
# 将浏览器日志发到后端，用 Python logger 打印出来，这样用户在
# ComfyUI 控制台（终端）也能看到画廊的日志。
# 独立插件使用自己的路由前缀，避免与其它插件（含原 ComfyUI-Danbooru-Gallery）冲突。
# ================================

@PromptServer.instance.routes.post("/danbooru_gallery_node/logs/batch")
async def js_log_batch(request):
    """接收前端 JS 日志，用 Python logger 转发到后端终端"""
    try:
        data = await request.json()
        logs = data.get("logs", [])
        for log_entry in logs:
            level = (log_entry.get("level") or "INFO").upper()
            component = log_entry.get("component", "danbooru_gallery")
            message = log_entry.get("message", "")
            # 映射级别
            if level in ("ERROR", "CRITICAL"):
                logger.error(f"[JS/{component}] {message}")
            elif level in ("WARN", "WARNING"):
                logger.warning(f"[JS/{component}] {message}")
            elif level == "DEBUG":
                logger.debug(f"[JS/{component}] {message}")
            else:
                logger.info(f"[JS/{component}] {message}")
    except Exception as e:
        logger.warning(f"[JSEndpoint] 日志转发错误: {e}")
    return web.json_response({"success": True})

# ================================
# Tag翻译API接口
# ================================

@PromptServer.instance.routes.get("/danbooru_gallery/translate_tag")
async def translate_tag_route(request):
    """翻译单个tag"""
    try:
        tag = request.query.get("tag", "").strip()
        if not tag:
            return web.json_response({"success": False, "error": "缺少tag参数"})
        
        translation = translation_system.translate_tag(tag)
        return web.json_response({
            "success": True,
            "tag": tag,
            "translation": translation
        })
    except Exception as e:
        logger.error(f"翻译tag接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)})

@PromptServer.instance.routes.post("/danbooru_gallery/translate_tags_batch")
async def translate_tags_batch_route(request):
    """批量翻译tags"""
    try:
        data = await request.json()
        tags = data.get("tags", [])
        
        if not isinstance(tags, list):
            return web.json_response({"success": False, "error": "tags必须是数组"})
        
        translations = translation_system.translate_tags_batch(tags)
        return web.json_response({
            "success": True,
            "translations": translations
        })
    except Exception as e:
        logger.error(f"批量翻译tags接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)})

class DanbooruGalleryNode:
    _post_cache = {}
    _post_cache_lock = threading.Lock()

    @classmethod
    def _get_cached_posts(cls, cache_key, max_age):
        with cls._post_cache_lock:
            cached = cls._post_cache.get(cache_key)
            if cached is None:
                return None
            cached_data, timestamp = cached
            if time.time() - timestamp < max_age:
                return cached_data
            cls._post_cache.pop(cache_key, None)
            return None

    @classmethod
    def _cache_posts(cls, cache_key, result_text, max_entries=200):
        with cls._post_cache_lock:
            cls._post_cache[cache_key] = (result_text, time.time())
            if len(cls._post_cache) > max_entries:
                oldest_key = min(cls._post_cache, key=lambda key: cls._post_cache[key][1])
                del cls._post_cache[oldest_key]

    TAG_CATEGORIES = ("artist", "copyright", "character", "general", "meta")

    @classmethod
    def _parse_selection_payload(cls, selection_data):
        """解析前端写入的 selection_data（容错：即使字符串被截断也要能拿到 token）。"""
        if isinstance(selection_data, dict):
            return dict(selection_data)
        text = str(selection_data or "").strip()
        if not text:
            return {}
        data = None
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning(
                "[GalleryNode] selection_data 不是完整 JSON(%s)，尝试提取 token；"
                "原文前 160 字符: %r", exc, text[:160]
            )
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {"selections": data}
        payload = {}
        token_match = re.search(r'"token"\s*:\s*"([A-Za-z0-9_\-]{8,})"', text)
        if token_match:
            payload["token"] = token_match.group(1)
        queue_match = re.search(r'"queue_id"\s*:\s*(\d+)', text)
        if queue_match:
            payload["queue_id"] = int(queue_match.group(1))
        return payload

    @staticmethod
    def _pick_image_url(post):
        """从站点返回的 post 结构里挑一个可下载的图片地址。"""
        if not isinstance(post, dict):
            return ""
        for key in ("file_url", "large_file_url", "sample_url", "preview_file_url"):
            value = post.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
        return ""

    @classmethod
    def _fetch_post_by_id(cls, post_id, source):
        """按 id 取回单个帖子（前端只传了 post_id 时用它兜底补全提示词/图片）。"""
        post_id = str(post_id or "").strip()
        if not post_id.isdigit():
            return None
        try:
            result_text = cls.get_posts_internal(
                f"id:{post_id}",
                1,
                1,
                source=source,
                force_public_detail=(source == "gelbooru"),
            )
            posts = json.loads(result_text) if result_text else []
        except Exception as exc:
            logger.warning(f"[GalleryNode] 按 id 获取帖子失败 id={post_id} source={source}: {exc}")
            return None
        for post in posts or []:
            if isinstance(post, dict) and not post.get("error") and str(post.get("id")) == post_id:
                return post
        for post in posts or []:
            if isinstance(post, dict) and not post.get("error"):
                return post
        return None

    @staticmethod
    def _normalize_filter_tag(tag):
        return re.sub(r"[,\s]+", "_", str(tag or "").strip().lower()).strip("_")

    @classmethod
    def _build_prompt_from_post(cls, post):
        """兜底：按当前 UI 设置从帖子的标签重建提示词。"""
        if not isinstance(post, dict):
            return ""
        settings = load_ui_settings()
        categories = [c for c in (settings.get("selected_categories") or []) if c in cls.TAG_CATEGORIES]
        if not categories:
            categories = list(cls.TAG_CATEGORIES)
        filter_tags, filter_enabled = load_filter_tags()
        filter_set = (
            {cls._normalize_filter_tag(t) for t in filter_tags}
            if filter_enabled and filter_tags else None
        )

        ordered_tags = []
        seen = set()
        for category in cls.TAG_CATEGORIES:
            for tag in str(post.get(f"tag_string_{category}") or "").split(" "):
                tag = tag.strip()
                if not tag or tag in seen:
                    continue
                seen.add(tag)
                ordered_tags.append((category, tag))
        if not ordered_tags:
            for tag in str(post.get("tag_string") or "").split(" "):
                tag = tag.strip()
                if not tag or tag in seen:
                    continue
                seen.add(tag)
                ordered_tags.append(("general", tag))

        result = []
        for category, tag in ordered_tags:
            if category not in categories:
                continue
            if filter_set and cls._normalize_filter_tag(tag) in filter_set:
                continue
            formatted = tag.replace("_", " ").replace("(", "\\(").replace(")", "\\)")
            if formatted:
                result.append(formatted)
        return ", ".join(result)

    @staticmethod
    def _load_image_tensor(image_url):
        img_data = _fetch_supported_media(image_url)
        img = Image.open(io.BytesIO(img_data)).convert("RGB")
        img_array = np.array(img).astype(np.float32) / 255.0
        return torch.from_numpy(img_array)[None,]

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {},
            "optional": {
                # 兼容前端 bypass 解析：
                # 该节点原本只有 hidden 输入，某些前端 bypass 路径会在无可见输入时抛出
                # "No input found for flattened id ... slot [0]"。
                # 增加可选透传槽位后，bypass 时不会因缺少输入槽而直接报错。
                "bypass_image": ("IMAGE", {"forceInput": True}),
                "bypass_prompts": ("STRING", {"forceInput": True}),
            },
            "hidden": {
                "selection_data": ("STRING", {"default": "{}", "multiline": True, "forceInput": True}),
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("images", "prompts")
    OUTPUT_IS_LIST = (True, True)
    FUNCTION = "get_selected_data"
    CATEGORY = "danbooru"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, selection_data="{}", **kwargs):
        # 每次入队 token / queue_id 都会变，据此让节点重新执行，避免命中旧缓存。
        payload = cls._parse_selection_payload(selection_data)
        token = str(payload.get("token") or "")
        queue_id = payload.get("queue_id")
        if token or queue_id:
            ids = [
                str(entry.get("post_id") or entry.get("id") or "")
                for entry in (payload.get("selections") or [])
                if isinstance(entry, dict)
            ]
            return f"{token}:{queue_id}:{','.join(ids)}"
        return str(selection_data or "")

    def get_selected_data(self, selection_data="{}", **kwargs):
        """把选中帖子的图片与提示词输出成 (images, prompts) 列表。"""
        placeholder = torch.zeros(1, 1, 1, 3)
        try:
            payload = self._parse_selection_payload(selection_data)
            token = str(payload.get("token") or "")

            entries = None
            snapshot = load_selection_snapshot(token)
            if snapshot and isinstance(snapshot.get("selections"), list):
                entries = snapshot["selections"]
                logger.info(f"[GalleryNode] 使用快照 token={token} 共 {len(entries)} 条")
            elif isinstance(payload.get("selections"), list):
                entries = payload["selections"]

            if not entries:
                logger.warning(
                    "[GalleryNode] 没有收到选中的图片：selection_data=%r。"
                    "请先在画廊里选中图片再执行。", str(selection_data)[:160]
                )
                return ([placeholder], [""])

            fallback_source = (load_ui_settings().get("source_site") or "danbooru").lower()
            images = []
            prompts = []

            for raw_entry in entries:
                entry = raw_entry if isinstance(raw_entry, dict) else {"post_id": raw_entry}
                post_id = str(entry.get("post_id") or entry.get("id") or "").strip()
                source = str(entry.get("source_site") or fallback_source).lower()
                prompt = entry.get("prompt") or ""
                image_url = entry.get("image_url") or ""

                # 快照缺字段时（例如只传了 id / 前端只给了 post_id）按 id 回站里补齐
                prompt_ready = bool(entry.get("prompt_ready"))
                if (not prompt and not prompt_ready) or not image_url:
                    post = self._fetch_post_by_id(post_id, source)
                    if post:
                        if not prompt and not prompt_ready:
                            prompt = self._build_prompt_from_post(post)
                        if not image_url:
                            image_url = self._pick_image_url(post)

                prompts.append(str(prompt))

                if not image_url:
                    logger.warning(f"[GalleryNode] 帖子 {post_id} 没有可用的图片地址，已输出占位图")
                    images.append(placeholder)
                    continue
                try:
                    images.append(self._load_image_tensor(image_url))
                except Exception as exc:
                    logger.error(f"[GalleryNode] 图片下载失败 post={post_id} url={image_url}: {exc}")
                    images.append(placeholder)

            logger.info(f"[GalleryNode] 输出 {len(images)} 张图片 / {len(prompts)} 段提示词 (token={token or '-'})")
            return (images, prompts)

        except Exception as exc:
            logger.error(f"[GalleryNode] 选择处理错误: {exc}")
            import traceback
            logger.debug(traceback.format_exc())
            return ([placeholder], [""])
    
    @staticmethod
    def get_posts_internal(tags: str, limit: int = 100, page: int = 1, rating: str = None, source: str = "danbooru", gelbooru_display_all_site_content: bool = None, force_public_detail: bool = False, before_id=None, gelbooru_dedup_mode: str = "off"):
        settings = load_settings()
        cache_enabled = settings.get("cache_enabled", True)
        max_cache_age = settings.get("max_cache_age", 3600)
        persistent_cache_age = settings.get("persistent_post_cache_age", 2592000)
        adapter = get_site_adapter(source)
        if gelbooru_display_all_site_content is None:
            gelbooru_display_all_site_content = settings.get("gelbooru_display_all_site_content", False)

        if gelbooru_dedup_mode not in ("off", "on", "on_auth"):
            gelbooru_dedup_mode = "off"

        # before_id 分页：数字校验，防注入
        before_id = str(before_id).strip() if before_id else ""
        if before_id and not before_id.isdigit():
            before_id = ""
        if before_id and re.search(r'(?:^|\s)(?:order|ordfav|sort):', tags or ''):
            before_id = ""
        if before_id and not (
            adapter.key == "danbooru"
            or (adapter.key == "gelbooru" and gelbooru_dedup_mode in ("on", "on_auth"))
        ):
            before_id = ""

        credentials = get_site_credentials(adapter)
        has_gelbooru_creds = has_required_site_credentials(adapter, credentials)
        persistent_cache = get_gallery_post_cache() if adapter.key == "gelbooru" and cache_enabled else None
        persistent_source = (
            "gelbooru:display_all" if gelbooru_display_all_site_content else "gelbooru:default"
        )

        def enrich_gelbooru_posts(posts):
            if not persistent_cache or not posts:
                return posts
            try:
                normal_posts = [post for post in posts if isinstance(post, dict) and not post.get("error")]
                # Preserve error slots and ordering while replacing normal posts by ID.
                cached_by_id = {
                    str(post.get("id")): post
                    for post in persistent_cache.merge_cached_posts(
                        persistent_source,
                        normal_posts,
                        persistent_cache_age,
                    )
                }
                enriched = [
                    cached_by_id.get(str(post.get("id")), post)
                    if isinstance(post, dict) and not post.get("error")
                    else post
                    for post in posts
                ]
                return persistent_cache.classify_posts(persistent_source, enriched)
            except Exception as exc:
                logger.warning(f"[GelbooruCache] 本地分类/缓存读取失败，回退原始列表: {exc}")
                return posts

        # 单次搜索的词条数上限：来自设置（默认 2，付费账号可调高）
        max_tags = clamp_max_search_tags(settings.get("max_search_tags", 2))
        # Gelbooru cursor 模式（追加 id:<X）在站点侧仍占一个名额，匿名访问只有 2 个，
        # 所以未配置密钥时把上限压到 1；配置了密钥的账号放宽（沿用旧上限 10）。
        if adapter.key == "gelbooru" and gelbooru_dedup_mode in ("on", "on_auth"):
            if gelbooru_dedup_mode == "on_auth" and has_gelbooru_creds:
                max_tags = min(max_tags, 10)
            else:
                max_tags = min(max_tags, 1)
        # 创建缓存键（rating一化排序，避免不同排序命中两条缓存）
        rating_key = ','.join(sorted(r.strip().lower() for r in (rating or '').split(',') if r.strip()))
        site_options_key = ""
        if adapter.key == "gelbooru":
            site_options_key = "display_all" if gelbooru_display_all_site_content else "default"
            site_options_key += f"_dedup_{gelbooru_dedup_mode}"
        response_shape_key = "detail" if force_public_detail else "list"
        cache_key = f"{adapter.key}:{site_options_key}:{response_shape_key}:tags{max_tags}:{tags}:{limit}:{page}:{rating_key}:{before_id}"

        # 判断是否获取收藏列表，如果是清除缓存以避免相同的请求前端列表不更新
        match = re.search(r'\bordfav:([^\s]+)', tags)

        # 如果启用了缓存，则检查缓存
        if cache_enabled and not match:
            cached_data = DanbooruGalleryNode._get_cached_posts(cache_key, max_cache_age)
            if cached_data is not None:
                if adapter.key == "gelbooru" and not force_public_detail:
                    try:
                        cached_posts = json.loads(cached_data)
                        cached_data = json.dumps(enrich_gelbooru_posts(cached_posts), ensure_ascii=False)
                    except (TypeError, json.JSONDecodeError):
                        pass
                return (cached_data,)


        # Danbooru 有实测的“免费限定词”清单；Gelbooru 保守处理，只豁免 date:
        exempt_prefixes = DANBOORU_TAG_LIMIT_EXEMPT if adapter.key == "danbooru" else frozenset()
        final_tags, dropped_tags = build_query_tags(tags, max_tags, exempt_prefixes)
        if dropped_tags:
            logger.info(
                "[%s] 最多 %s 个搜索词，已忽略: %s"
                "（%s 类限定词不占名额，order:/sort:/user: 等会占名额）",
                adapter.key, max_tags, ' '.join(dropped_tags), "/".join(sorted(exempt_prefixes)) or "date:",
            )

        rating_query = ""
        if adapter.key == "danbooru" and rating and rating.lower() != 'all':
            allowed = {'general', 'sensitive', 'questionable', 'explicit', 'g', 's', 'q', 'e'}
            rating_values = [r.strip().lower() for r in rating.split(',') if r.strip()]
            rating_values = [r for r in rating_values if r in allowed]
            if len(rating_values) == 1:
                rating_query = f"rating:{rating_values[0]}"
            elif len(rating_values) > 1:
                rating_query = ' '.join(f"~rating:{r}" for r in rating_values)
        elif adapter.key != "danbooru":
            rating_query = rating
        
        tags = final_tags
        # Gelbooru cursor（id:<X）要追加到 tags（cursor 在 tags 之后，不是之前）
        # D站游标 page=b<id> 在下面单独处理，不拼 tags
        if before_id and adapter.key == "gelbooru":
            tags = f"{tags} id:<{before_id}".strip()

        username, api_key = load_user_auth()
        auth = HTTPBasicAuth(username, api_key) if adapter.requires_auth and username and api_key else None
        if adapter.key == "gelbooru" and force_public_detail:
            def is_queue_ready_detail(post):
                return bool(
                    isinstance(post, dict)
                    and post.get("file_url")
                    and post.get("_tag_categories_exact")
                    and not post.get("_gelbooru_preview_only")
                )

            requested_id_match = re.search(r"(?:^|\s)id:(\d+)(?:\s|$)", tags or "")
            if persistent_cache and requested_id_match:
                try:
                    cached_details = persistent_cache.get_posts(
                        persistent_source,
                        [requested_id_match.group(1)],
                        persistent_cache_age,
                    )
                    cached_post = cached_details.get(requested_id_match.group(1))
                    if is_queue_ready_detail(cached_post):
                        result_text = json.dumps([cached_post], ensure_ascii=False)
                        DanbooruGalleryNode._cache_posts(cache_key, result_text)
                        return (result_text,)
                except Exception as exc:
                    logger.warning(f"[GelbooruCache] 持久缓存读取失败，继续请求详情: {exc}")
            posts = _fetch_gelbooru_public_posts(
                adapter,
                tags,
                limit,
                page,
                rating_query,
                gelbooru_display_all_site_content,
                hydrate_details=True,
            )
            if persistent_cache:
                try:
                    for post in posts:
                        if is_queue_ready_detail(post):
                            persistent_cache.put_post(persistent_source, post)
                except Exception as exc:
                    logger.warning(f"[GelbooruCache] 详情持久化失败: {exc}")
            result_text = json.dumps(posts, ensure_ascii=False)
            if cache_enabled and posts and all(is_queue_ready_detail(post) for post in posts):
                DanbooruGalleryNode._cache_posts(cache_key, result_text)
            return (result_text,)

        if not has_required_site_credentials(adapter, credentials):
            if adapter.key == "gelbooru":
                logger.info("[Gelbooru] 未配置 User ID/API Key，改用公开网页解析模式")
                posts = _fetch_gelbooru_public_posts(adapter, tags, limit, page, rating_query, gelbooru_display_all_site_content)
                posts = enrich_gelbooru_posts(posts)
                result_text = json.dumps(posts, ensure_ascii=False)

                if cache_enabled and not (adapter.key == "gelbooru" and gelbooru_display_all_site_content and not posts):
                    # 跳过 error 占位格缓存（否则一次限流会让接下来 TTL 内都看不到新图）
                    has_error_placeholder = posts and any(isinstance(p, dict) and p.get("error") for p in posts)
                    if not has_error_placeholder:
                        DanbooruGalleryNode._cache_posts(cache_key, result_text)

                return (result_text,)
            logger.warning(f"[{adapter.key}] 缺少必要认证信息，已跳过帖子请求")
            return ("[]",)
            
        params = adapter.build_posts_params(tags, limit, page, rating_query)
        params = adapter.apply_auth_params(params, credentials)
        # D站游标 page=b<id>，G站已通过 tags 注入
        if before_id and adapter.key == "danbooru":
            params["page"] = f"b{before_id}"

        try:
            if adapter.key == "gelbooru":
                response = _gelbooru_request("GET", adapter.posts_url, request_kind="api", params=params, timeout=15)
            else:
                response = _danbooru_request("GET", adapter.posts_url, params=params, auth=auth, timeout=15)
            if response.status_code == 401 and adapter.key == "gelbooru":
                logger.warning("[Gelbooru] DAPI 返回 401，改用公开网页解析模式")
                posts = _fetch_gelbooru_public_posts(adapter, tags, limit, page, rating_query, gelbooru_display_all_site_content)
            else:
                response.raise_for_status()
                posts = adapter.normalize_posts_response(response.json())

            if adapter.key == "gelbooru":
                posts = enrich_gelbooru_posts(posts)

            result_text = json.dumps(posts, ensure_ascii=False)

            if cache_enabled and not (adapter.key == "gelbooru" and gelbooru_display_all_site_content and not posts):
                has_error_placeholder = posts and any(isinstance(p, dict) and p.get("error") for p in posts)
                if not has_error_placeholder:
                    DanbooruGalleryNode._cache_posts(cache_key, result_text)

            return (result_text,)
        except requests.Timeout as e:
            logger.error(f"请求超时: {e}")
            payload = build_gallery_error_payload(adapter.key, "加载图片列表", exc=e, retries_exhausted=True, retry_count=1)
            return (json.dumps([payload], ensure_ascii=False),)
        except requests.RequestException as e:
            logger.error(f"请求异常: {e}")
            payload = build_gallery_error_payload(adapter.key, "加载图片列表", exc=e, retries_exhausted=True, retry_count=1)
            return (json.dumps([payload], ensure_ascii=False),)
        except Exception as e:
            logger.error(f"未知错误: {e}")
            payload = build_gallery_error_payload(adapter.key, "加载图片列表", exc=e, retries_exhausted=True, retry_count=1)
            return (json.dumps([payload], ensure_ascii=False),)

# 启动时应用 settings.json 里的日志开关/级别（设置面板改完会立即再应用一次）
try:
    apply_log_settings()
except Exception as _log_config_error:  # 日志配置失败不能影响插件加载
    logger.warning(f"[LogConfig] 应用日志设置失败: {_log_config_error}")

# ComfyUI 必须的字典
def get_node_class_mappings():
    return {
        "DanbooruGalleryNode": DanbooruGalleryNode
    }

def get_node_display_name_mappings():
    return {
        "DanbooruGalleryNode": "D站画廊 (Danbooru Gallery)"
    }

NODE_CLASS_MAPPINGS = get_node_class_mappings()
NODE_DISPLAY_NAME_MAPPINGS = get_node_display_name_mappings()

