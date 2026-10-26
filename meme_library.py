"""User-imported reaction images, matched by names and tags (not image analysis)."""
from __future__ import annotations

import copy
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import tempfile
import warnings

from PIL import Image, UnidentifiedImageError

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_IMPORT_FILES = 20
MAX_LIBRARY_ITEMS = 1000
_MAX_PIXELS = 20_000_000
_MAX_ANIMATION_PIXELS = 100_000_000
_MAX_INDEX_BYTES = 2 * 1024 * 1024
_SCHEMA = "jiehua.meme-library"
_EXTENSIONS = {"PNG": ".png", "JPEG": ".jpg", "GIF": ".gif", "WEBP": ".webp"}
_ID = re.compile(r"[0-9a-f]{64}\Z")
_TAG_ALIASES = {
    "抽象": ("抽象", "离谱", "无厘头", "发疯"),
    "搞怪": ("搞怪", "沙雕", "整活", "鬼畜"),
    "无语": ("无语", "无奈", "沉默", "扶额"),
    "开心": ("开心", "哈哈", "笑死", "高兴", "快乐"),
    "拒绝": ("拒绝", "不要", "不行", "拒收"),
    "尴尬": ("尴尬", "社死", "害羞"),
    "撒娇": ("撒娇", "卖萌", "贴贴", "可爱"),
    "夸张": ("夸张", "震惊", "惊讶", "震撼"),
    "赞同": ("赞同", "同意", "点赞", "支持"),
    "安慰": ("安慰", "抱抱", "心疼"),
}
BUILTIN_TAGS = tuple(_TAG_ALIASES)


def _clean_name(value: str) -> str:
    return "".join(char for char in value if char.isprintable()).strip()[:100] or "表情图片"


def _tags(value: str) -> list[str]:
    if not isinstance(value, str) or len(value) > 200:
        raise ValueError("表情标签请填写最多 200 字，用逗号或空格分隔。")
    result = []
    for tag in re.split(r"[,，;；\s]+", value.strip()):
        if not tag:
            continue
        if len(tag) > 16 or not all(char.isprintable() for char in tag):
            raise ValueError("每个表情标签最多 16 字，不能包含控制字符。")
        if tag not in result:
            result.append(tag)
    if len(result) > 10:
        raise ValueError("每张表情最多设置 10 个标签。")
    return result


def _inferred_tags(text: str) -> list[str]:
    return [tag for tag, aliases in _TAG_ALIASES.items() if any(alias in text for alias in aliases)]


def inspect_image_bytes(raw: bytes) -> dict:
    """Validate image bytes without disk/network access or changing animation."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_FILE_BYTES:
        raise ValueError("图片为空或超过 10 MB，未导入。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as image:
                image_format = image.format
                width, height = image.size
                if image_format not in _EXTENSIONS:
                    raise ValueError("仅支持 PNG、JPEG、GIF 和 WebP 图片。")
                if not 1 <= width <= 10000 or not 1 <= height <= 10000 or width * height > _MAX_PIXELS:
                    raise ValueError("图片尺寸过大，单张最多 2000 万像素且边长不超过 10000。")
                frames = getattr(image, "n_frames", 1)
                if frames > 300 or frames * width * height > _MAX_ANIMATION_PIXELS:
                    raise ValueError("动图过长或尺寸过大，请选择较小的表情。")
                for frame in range(frames):
                    image.seek(frame)
                    image.load()
                return {"width": width, "height": height, "format": image_format,
                        "animated": frames > 1, "extension": _EXTENSIONS[image_format]}
    except (UnidentifiedImageError, OSError, SyntaxError, EOFError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise ValueError("图片无法完整解码或尺寸过大，请选择有效的 PNG、JPEG、GIF 或 WebP 图片。") from None


class MemeLibrary:
    """A local, opt-in image library. Construction never creates files.

    Only explicitly imported files are copied. Recommendations use the supplied
    hint/tags and file names; they do not inspect the meaning of image pixels.
    """

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self._index = self.directory / "index.json"
        self._items: list[dict] = []
        self._index_digest: str | None = None
        self._check_storage()
        if self._index.exists():
            self._load()

    def _check_storage(self):
        if self.directory.is_symlink() or self._index.is_symlink():
            raise ValueError("表情库或索引是符号链接，未读取或修改。")
        if self.directory.exists() and not self.directory.is_dir():
            raise ValueError("表情库位置不是文件夹，请选择其他位置。")

    def _entry_path(self, item: dict) -> Path:
        path = self.directory / (item["id"] + item["extension"])
        if path.is_symlink():
            raise ValueError("表情文件是符号链接，未读取或修改。")
        return path

    def _current_index(self) -> bytes | None:
        self._check_storage()
        if not self._index.exists():
            return None
        try:
            if not self._index.is_file() or self._index.stat().st_size > _MAX_INDEX_BYTES:
                raise OSError("invalid index")
            return self._index.read_bytes()
        except OSError:
            raise ValueError("表情索引无法读取，未覆盖原文件。") from None

    def _assert_current(self):
        raw = self._current_index()
        digest = hashlib.sha256(raw).hexdigest() if raw is not None else None
        if digest != self._index_digest:
            raise ValueError("表情库已被其他操作修改，请关闭表情库窗口后重新打开。")

    def _load(self):
        try:
            raw = self._current_index()
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("schema") != _SCHEMA or type(data.get("version")) is not int or data["version"] != 1:
                raise ValueError("invalid schema")
            items = data.get("items")
            if not isinstance(items, list) or len(items) > MAX_LIBRARY_ITEMS:
                raise ValueError("invalid items")
            validated = []
            ids = set()
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not _ID.fullmatch(item["id"]) or item["id"] in ids:
                    raise ValueError("invalid id")
                name, tags, image_format = item.get("name"), item.get("tags"), item.get("format")
                if not isinstance(name, str) or not name or name != _clean_name(name) or "/" in name or "\\" in name:
                    raise ValueError("invalid name")
                if not isinstance(tags, list) or len(tags) > 10 or any(not isinstance(tag, str) or not tag or len(tag) > 16 or not all(char.isprintable() and not char.isspace() for char in tag) or any(char in ",，;；" for char in tag) for tag in tags) or len(set(tags)) != len(tags):
                    raise ValueError("invalid tags")
                if not isinstance(image_format, str) or image_format not in _EXTENSIONS or item.get("extension") != _EXTENSIONS[image_format] or type(item.get("animated")) is not bool:
                    raise ValueError("invalid format")
                width, height = item.get("width"), item.get("height")
                if type(width) is not int or type(height) is not int or not 1 <= width <= 10000 or not 1 <= height <= 10000 or width * height > _MAX_PIXELS:
                    raise ValueError("invalid dimensions")
                clean = {key: copy.deepcopy(item[key]) for key in ("id", "name", "tags", "width", "height", "format", "animated", "extension")}
                self._entry_path(clean)
                validated.append(clean)
                ids.add(clean["id"])
            self._items = validated
            self._index_digest = hashlib.sha256(raw).hexdigest()
        except (OSError, ValueError, TypeError, UnicodeError):
            raise ValueError("表情索引损坏或格式不支持，未覆盖原文件。请备份后选择新的表情库位置。") from None

    def _read_source(self, path: Path) -> bytes:
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("请选择小于等于 10 MB 的本地图片文件，不能使用符号链接。")
            with path.open("rb") as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
            if not raw or len(raw) > MAX_FILE_BYTES:
                raise ValueError("图片为空或超过 10 MB，未导入。")
            return raw
        except OSError:
            raise ValueError("无法读取选中的图片，请检查文件是否存在及读取权限。") from None

    def _inspect(self, raw: bytes) -> dict:
        return inspect_image_bytes(raw)

    def _save(self, items: list[dict]):
        temporary = None
        self._assert_current()
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._check_storage()
            payload = json.dumps({"schema": _SCHEMA, "version": 1, "items": items}, ensure_ascii=False).encode("utf-8")
            with tempfile.NamedTemporaryFile(mode="wb", dir=self.directory, prefix=".meme-index-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            self._assert_current()
            os.replace(temporary, self._index)
            self._index_digest = hashlib.sha256(payload).hexdigest()
            self._items = items
        except OSError:
            raise ValueError("无法保存表情库，请检查文件夹位置及写入权限。") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def import_files(self, paths: list[Path], tags: str = "") -> list[dict]:
        """Validate the full batch, then copy its images without re-encoding them."""
        if not isinstance(paths, (list, tuple)) or not 1 <= len(paths) <= MAX_IMPORT_FILES:
            raise ValueError("每次请选择 1 至 20 张表情图片。")
        user_tags = _tags(tags)
        self._assert_current()
        pending = []
        items = copy.deepcopy(self._items)
        for source in paths:
            try:
                source = Path(source)
            except TypeError:
                raise ValueError("请选择有效的本地图片路径。") from None
            raw = self._read_source(source)
            details = self._inspect(raw)
            image_id = hashlib.sha256(raw).hexdigest()
            name = _clean_name(source.name).replace("/", "_").replace("\\", "_")
            combined_tags = list(dict.fromkeys(user_tags + _inferred_tags(name)))[:10]
            existing = next((item for item in items if item["id"] == image_id), None)
            if existing is not None:
                items.remove(existing)
                existing["tags"] = list(dict.fromkeys(existing["tags"] + combined_tags))[:10]
                item = existing
            else:
                item = {"id": image_id, "name": name, "tags": combined_tags, **details}
            items.append(item)
            pending.append((source, item))
        if len(items) > MAX_LIBRARY_ITEMS:
            raise ValueError("表情库最多保存 1000 张图片，请先移除不需要的表情。")
        created = []
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._check_storage()
            for source, item in pending:
                target = self._entry_path(item)
                raw = self._read_source(source)
                if hashlib.sha256(raw).hexdigest() != item["id"]:
                    raise ValueError("导入期间图片发生变化，请重新选择图片。")
                if target.exists():
                    if not target.is_file() or hashlib.sha256(self._read_source(target)).hexdigest() != item["id"]:
                        raise ValueError("表情库中的同名文件已被修改，未覆盖该文件。")
                    continue
                with target.open("xb") as stream:
                    created.append(target)
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            self._save(items)
        except (OSError, ValueError) as error:
            for path in created:
                if not path.is_symlink():
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
            if isinstance(error, ValueError):
                raise
            raise ValueError("无法导入表情，请检查文件夹写入权限。") from None
        ids = list(dict.fromkeys(item["id"] for _, item in pending))
        return [copy.deepcopy(next(item for item in items if item["id"] == image_id)) for image_id in ids]

    def list(self) -> list[dict]:
        """Return independent metadata copies, most recently imported first."""
        self._assert_current()
        for item in self._items:
            self._entry_path(item)
        return copy.deepcopy(self._items[::-1])

    def recommend(self, query: str, tags: list[str] | None = None, limit: int = 3) -> list[dict]:
        """Match hint/tag text; when nothing matches, offer recent curated files."""
        if not isinstance(query, str) or len(query) > 2000:
            raise ValueError("表情提示请使用最多 2000 字的文字。")
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("每次可推荐 1 至 10 张表情。")
        if tags is not None and (not isinstance(tags, list) or len(tags) > 10 or any(not isinstance(tag, str) or not tag or len(tag) > 16 for tag in tags)):
            raise ValueError("表情推荐标签最多 10 个，每个最多 16 字。")
        query = query.casefold()
        terms = list(dict.fromkeys([tag.casefold() for tag in (tags or [])] + _inferred_tags(query) + re.findall(r"[\w\u4e00-\u9fff]+", query)))
        ranked = []
        for recency, item in enumerate(self.list()):
            name = Path(item["name"]).stem.casefold()
            item_tags = [tag.casefold() for tag in item["tags"]]
            score = sum(5 if term in item_tags else 2 if term in name else 1 if any(term in tag or tag in term for tag in item_tags) else 0 for term in terms)
            # A natural language hint may contain a custom tag or complete name.
            score += sum(2 for tag in item_tags if tag in query)
            if name and name in query:
                score += 2
            ranked.append((score, recency, item))
        ranked.sort(key=lambda candidate: (-candidate[0], candidate[1]))
        return [item for _, _, item in ranked[:limit]]

    def get_path(self, image_id: str) -> Path:
        """Return only a known, unmodified owned image path, never an input path."""
        self._assert_current()
        if not isinstance(image_id, str) or not _ID.fullmatch(image_id):
            raise ValueError("无效的表情编号。")
        item = next((item for item in self._items if item["id"] == image_id), None)
        if item is None:
            raise ValueError("表情不在当前表情库中。")
        path = self._entry_path(item)
        if hashlib.sha256(self._read_source(path)).hexdigest() != image_id:
            raise ValueError("表情库文件已被修改，请重新导入原图片。")
        return path

    def remove(self, image_id: str) -> bool:
        """Remove a known library copy; the originally selected file is untouched."""
        self._assert_current()
        if not isinstance(image_id, str) or not _ID.fullmatch(image_id):
            raise ValueError("无效的表情编号。")
        item = next((item for item in self._items if item["id"] == image_id), None)
        if item is None:
            return False
        path = self._entry_path(item)
        # A missing copy can still be removed from the index. Refuse to delete
        # an unrelated file that was placed over a known copy by another program.
        if path.exists():
            self.get_path(image_id)
        remaining = [entry for entry in self._items if entry["id"] != image_id]
        backup = None
        try:
            if path.exists():
                with tempfile.NamedTemporaryFile(dir=self.directory, prefix=".meme-remove-", suffix=".tmp", delete=False) as stream:
                    backup = Path(stream.name)
                os.replace(path, backup)
            self._save(remaining)
        except (OSError, ValueError) as error:
            if backup is not None and backup.exists():
                try:
                    if path.exists() or path.is_symlink():
                        raise OSError("original path occupied")
                    os.replace(backup, path)
                except OSError:
                    raise ValueError("移除失败，表情副本仍保存在表情库的 .meme-remove- 临时文件中，未删除。") from None
            if isinstance(error, ValueError):
                raise
            raise ValueError("无法移除表情，请检查表情库写入权限。") from None
        if backup is not None:
            try:
                backup.unlink()
            except OSError:
                pass
        return True
