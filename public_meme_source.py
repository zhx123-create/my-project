"""Optional, on-click public meme packs; matching stays on this computer.

Only fixed ChineseBQB GitHub release assets are fetched. No query, chat text,
model credentials or local images are sent to the public image source.
"""
from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import PurePosixPath
import re
import stat
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import zipfile

from meme_library import MAX_FILE_BYTES, inspect_image_bytes

SOURCE = "ChineseBQB 公开图库"
SOURCE_PAGE = "https://github.com/zhaoolee/ChineseBQB"
_RELEASE_ROOT = SOURCE_PAGE + "/releases/download/bqb-downloads/"
_MAX_PACK_BYTES = 12 * 1024 * 1024
_MAX_UNPACKED_BYTES = 50 * 1024 * 1024
_PACKS = (
    {"key": "panda", "name": "熊猫头", "tags": ("熊猫头", "搞怪", "抽象"),
     "aliases": ("熊猫", "熊猫头", "金馆长"), "asset": "bqb-015-473f1fd0cb35ff60.zip"},
    {"key": "penguin", "name": "沙雕企鹅", "tags": ("企鹅", "搞怪", "抽象"),
     "aliases": ("企鹅", "沙雕企鹅"), "asset": "bqb-031-dc81e54af6b7711b.zip"},
    {"key": "funny", "name": "滑稽大佬", "tags": ("滑稽", "搞怪", "抽象"),
     "aliases": ("滑稽", "整活", "鬼畜"), "asset": "bqb-001-ec22fc6ed4dcdae6.zip"},
    {"key": "cat", "name": "猫咪日常", "tags": ("猫咪", "搞怪", "可爱"),
     "aliases": ("猫", "猫咪", "可爱", "卖萌"), "asset": "bqb-049-45c73042dd0a9575.zip"},
)
_PACK_URLS = frozenset(_RELEASE_ROOT + pack["asset"] for pack in _PACKS)
_IMAGE_SUFFIXES = frozenset((".png", ".jpg", ".jpeg", ".gif", ".webp"))


class PublicMemeError(ValueError):
    """A readable source/network error that does not contain signed URLs."""


def _allowed_url(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.fragment:
            return False
        return url in _PACK_URLS or (
            parsed.hostname == "release-assets.githubusercontent.com"
            and parsed.path.startswith("/github-production-release-asset/185535659/")
        )
    except (ValueError, TypeError):
        return False


class _SourceRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _allowed_url(newurl):
            raise PublicMemeError("公开图库跳转到未支持的地址，已停止下载。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url):
    if url not in _PACK_URLS:
        raise PublicMemeError("只能读取已配置的公开表情分类包。")
    opener = urllib.request.build_opener(_SourceRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": "JieHua/1.0", "Accept": "application/zip"})
    started = time.monotonic()
    try:
        with opener.open(request, timeout=12) as response:
            if not _allowed_url(response.geturl()):
                raise PublicMemeError("公开图库地址不受支持，已停止下载。")
            size = response.headers.get("Content-Length")
            if size is not None and (not size.isdigit() or int(size) > _MAX_PACK_BYTES):
                raise PublicMemeError("公开表情分类包超过 12 MB，未下载。")
            parts, total = [], 0
            while True:
                if time.monotonic() - started > 25:
                    raise PublicMemeError("公开图库下载超时，请稍后重试或导入本地图片。")
                chunk = response.read(min(65536, _MAX_PACK_BYTES + 1 - total))
                if not chunk:
                    return b"".join(parts)
                parts.append(chunk)
                total += len(chunk)
                if total > _MAX_PACK_BYTES:
                    raise PublicMemeError("公开表情分类包超过 12 MB，未下载。")
    except (urllib.error.URLError, OSError, TimeoutError):
        raise PublicMemeError("暂时无法连接 GitHub 公开图库。请检查网络，或先导入自己的表情图片。") from None


def _keywords(query, tags):
    words = list(tags)
    for token in re.findall(r"[\w\u4e00-\u9fff]+", query):
        if len(token) <= 16:
            words.append(token)
        for length in (2, 3):
            words.extend(token[index:index + length] for index in range(len(token) - length + 1))
    return list(dict.fromkeys(word.casefold() for word in words if len(word) >= 2))[:160]


def _safe_member(info):
    name = info.filename
    path = PurePosixPath(name)
    mode = info.external_attr >> 16
    return (
        not info.is_dir() and not path.is_absolute() and ".." not in path.parts
        and "\\" not in name and ":" not in name and len(name) <= 300
        and all(char.isprintable() for char in name)
        and not stat.S_ISLNK(mode) and not (info.flag_bits & 1)
        and path.suffix.casefold() in _IMAGE_SUFFIXES
        and 0 < info.file_size <= MAX_FILE_BYTES
        and info.file_size <= max(1, info.compress_size) * 200
    )


class PublicMemeSource:
    """Keep downloaded packs in RAM; each search reads at most one fixed pack.

    Results are category/filename matches, not semantic image recognition.
    Repeating a search rotates categories tied for relevance and candidate images.
    Original bytes are returned so saving a GIF keeps its animation.
    """

    def __init__(self):
        self._cache = {}
        self._search_number = 0
        self._offsets = {}

    def search(self, query: str, tags: list[str] | None = None, limit=3) -> list[dict]:
        if not isinstance(query, str) or len(query) > 500 or not all(char.isprintable() or char.isspace() for char in query):
            raise PublicMemeError("检索主题请填写最多 500 字的文字。")
        if tags is None:
            tags = []
        if not isinstance(tags, list) or len(tags) > 10 or any(not isinstance(tag, str) or not tag.strip() or len(tag) > 16 or not all(char.isprintable() for char in tag) for tag in tags):
            raise PublicMemeError("表情关键词最多 10 个，每个最多 16 字。")
        if type(limit) is not int or not 1 <= limit <= 9:
            raise PublicMemeError("每次可展示 1 至 9 张候选。")
        text = " ".join((query.strip(), *tags)).casefold()
        if not text.strip():
            return []
        keywords = _keywords(query, tags)
        scores = [sum(4 for alias in pack["aliases"] if alias.casefold() in text) for pack in _PACKS]
        best = max(scores)
        choices = [pack for pack, score in zip(_PACKS, scores) if score == best]
        pack = choices[self._search_number % len(choices)]
        self._search_number += 1
        url = _RELEASE_ROOT + pack["asset"]
        if pack["key"] not in self._cache:
            raw = _download(url)
            if len(raw) > _MAX_PACK_BYTES:
                raise PublicMemeError("公开表情分类包超过 12 MB，未读取。")
            self._cache[pack["key"]] = raw
        try:
            with zipfile.ZipFile(BytesIO(self._cache[pack["key"]])) as archive:
                entries = archive.infolist()
                if len(entries) > 1000 or sum(info.file_size for info in entries) > _MAX_UNPACKED_BYTES:
                    raise PublicMemeError("公开表情分类包内容过大，未读取。")
                ranked = []
                for info in entries:
                    if not _safe_member(info):
                        continue
                    name = PurePosixPath(info.filename).name
                    # Packs prefix every name with e.g. 金馆长熊猫00041. Do not
                    # mislabel that shared category prefix as a semantic match.
                    descriptive_name = re.sub(r"^[^\W\d_]*\d+[-_ ]*", "", PurePosixPath(name).stem).casefold()
                    score = sum(len(word) for word in keywords if word in descriptive_name)
                    ranked.append((score, name, info))
                ranked.sort(key=lambda entry: (-entry[0], entry[1]))
                # Preserve actual filename matches before browsing category fallbacks.
                if any(score for score, _, _ in ranked):
                    ranked = [item for item in ranked if item[0]]
                offset_key = (pack["key"], text)
                offset = self._offsets.get(offset_key, 0) % max(1, len(ranked))
                ranked = ranked[offset:] + ranked[:offset]
                results, seen = [], set()
                for score, name, info in ranked[:max(limit * 5, 30)]:
                    try:
                        raw = archive.read(info)
                        details = inspect_image_bytes(raw)
                    except (ValueError, OSError, EOFError, zipfile.BadZipFile, RuntimeError):
                        continue
                    image_id = hashlib.sha256(raw).hexdigest()
                    if image_id in seen:
                        continue
                    seen.add(image_id)
                    clean_name = (pack["name"] + "-" + name)[:100]
                    results.append({"id": image_id, "name": clean_name, "tags": list(pack["tags"]),
                                    **details, "data": raw, "source": SOURCE, "source_url": url,
                                    "reason": "文件名匹配，请看图确认" if score else "分类匹配，请看图确认"})
                    if len(results) == limit:
                        break
                self._offsets[offset_key] = offset + max(1, len(results))
                return results
        except (zipfile.BadZipFile, OSError, EOFError):
            self._cache.pop(pack["key"], None)
            raise PublicMemeError("公开表情分类包暂时无法读取，请稍后重试。") from None
