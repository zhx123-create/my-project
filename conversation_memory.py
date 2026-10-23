"""Explicitly imported per-conversation context, with opt-in local persistence."""
import copy
import json
import os
from pathlib import Path
import tempfile

MAX_HISTORY_CHARS = 12000
MAX_PROFILES = 100
MAX_ENTRIES = 60
_SCHEMA = "jiehua.conversation-memory"


def _text(value, limit, label, allow_empty=True):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{label}必须是最多 {limit} 字的文字。")
    value = value.replace("\r\n", "\n").strip()
    if not allow_empty and not value:
        raise ValueError(f"请填写{label}。")
    return value


def _line_overlap(previous, current):
    """Find suffix/prefix overlap in linear time, including repeated lines."""
    old, new = previous.splitlines(), current.splitlines()
    prefix = [0] * len(new)
    for index in range(1, len(new)):
        matched = prefix[index - 1]
        while matched and new[index] != new[matched]:
            matched = prefix[matched - 1]
        if new[index] == new[matched]:
            matched += 1
        prefix[index] = matched
    matched = 0
    for line in old:
        if matched == len(new):
            matched = prefix[matched - 1]
        while matched and line != new[matched]:
            matched = prefix[matched - 1]
        if line == new[matched]:
            matched += 1
    return "\n".join(new[matched:]).strip()


class ConversationMemory:
    def __init__(self, persistence_path=None):
        self.path = Path(persistence_path) if persistence_path is not None else None
        self.persistent = False
        self._profiles = {}
        if self.path is not None and self.path.is_symlink():
            raise ValueError("记忆文件是符号链接，未读取或修改。")
        if self.path is not None and self.path.exists():
            self._load()

    def names(self):
        return list(self._profiles)

    def _ensure(self, name):
        name = _text(name, 100, "会话名称", allow_empty=False)
        if name not in self._profiles:
            if len(self._profiles) >= MAX_PROFILES:
                raise ValueError("最多保存 100 个会话，请先删除不需要的会话。")
            self._profiles[name] = {"name": name, "background": "", "relationship": "", "goal": "", "history": [], "last_snapshot": ""}
            self._save()
        return self._profiles[name]

    def profile(self, name):
        profile = copy.deepcopy(self._ensure(name))
        profile.pop("last_snapshot", None)
        return profile

    def update_profile(self, name, *, background=None, relationship=None, goal=None):
        updates = {}
        for key, value, limit, label in (("background", background, 3000, "背景"), ("relationship", relationship, 100, "关系"), ("goal", goal, 1000, "沟通目标")):
            if value is not None:
                updates[key] = _text(value, limit, label)
        profile = self._ensure(name)
        profile.update(updates)
        self._save()
        return self.profile(name)

    def _trim(self, profile):
        history = profile["history"]
        while len(history) > MAX_ENTRIES:
            history.pop(0)
        while len(history) > 1 and sum(len(item["text"]) + 40 for item in history) > MAX_HISTORY_CHARS:
            history.pop(0)
        if history and len(history[0]["text"]) > MAX_HISTORY_CHARS - 40:
            history[0]["text"] = history[0]["text"][-(MAX_HISTORY_CHARS - 40):]

    def add_context(self, name, text):
        """Append only new lines when successive full snapshots overlap."""
        text = _text(text, 20000, "聊天内容", allow_empty=False)
        profile = self._ensure(name)
        previous = profile["last_snapshot"]
        if text == previous:
            return False
        addition = _line_overlap(previous, text)
        profile["last_snapshot"] = text
        if addition:
            profile["history"].append({"kind": "context", "text": addition})
            self._trim(profile)
        self._save()
        return bool(addition)

    def confirm_sent(self, name, text):
        """Only call for a user explicitly confirming an actually sent reply."""
        text = _text(text, 3000, "已发送内容", allow_empty=False)
        profile = self._ensure(name)
        entry = {"kind": "sent", "text": text}
        if profile["history"] and profile["history"][-1] == entry:
            return False
        profile["history"].append(entry)
        self._trim(profile)
        self._save()
        return True

    def history_text(self, name):
        profile = self._ensure(name)
        labels = {"context": "用户导入的聊天", "sent": "用户确认已发送（我）"}
        return "\n\n".join(f"【{labels[item['kind']]}】\n{item['text']}" for item in profile["history"])

    def clear_history(self, name):
        profile = self._ensure(name)
        profile["history"] = []
        profile["last_snapshot"] = ""
        self._save()

    def delete(self, name):
        name = _text(name, 100, "会话名称", allow_empty=False)
        existed = self._profiles.pop(name, None) is not None
        if existed:
            self._save()
        return existed

    def set_persistent(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("本地保存开关必须为布尔值。")
        if enabled and self.path is None:
            raise ValueError("尚未指定本地记忆文件位置，不能启用保存。")
        if self.path is not None and self.path.is_symlink():
            raise ValueError("记忆文件是符号链接，未修改。")
        if not enabled:
            if self.path is not None:
                try:
                    self.path.unlink(missing_ok=True)
                except OSError:
                    raise ValueError("无法删除本地记忆文件，请检查文件权限后重试。") from None
            self.persistent = False
            return
        previous = self.persistent
        self.persistent = True
        try:
            self._save()
        except ValueError:
            self.persistent = previous
            raise

    def _save(self):
        if not self.persistent:
            return
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_symlink():
                raise OSError("refusing symlink")
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, prefix=".jiehua-memory-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({"schema": _SCHEMA, "version": 1, "persistent": True, "profiles": list(self._profiles.values())}, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError:
            raise ValueError("无法保存本地会话记忆，请检查文件位置和权限。") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _load(self):
        try:
            if self.path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("oversized memory")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("schema") != _SCHEMA or data.get("version") != 1 or type(data.get("persistent")) is not bool:
                raise ValueError("unknown memory schema")
            if not data["persistent"]:
                return
            profiles = data.get("profiles")
            if not isinstance(profiles, list) or len(profiles) > MAX_PROFILES:
                raise ValueError("invalid profiles")
            loaded = {}
            for raw in profiles:
                if not isinstance(raw, dict):
                    raise ValueError("invalid profile")
                name = _text(raw.get("name"), 100, "会话名称", allow_empty=False)
                if name in loaded:
                    raise ValueError("duplicate profile")
                profile = {"name": name, "background": _text(raw.get("background", ""), 3000, "背景"), "relationship": _text(raw.get("relationship", ""), 100, "关系"), "goal": _text(raw.get("goal", ""), 1000, "沟通目标"), "last_snapshot": _text(raw.get("last_snapshot", ""), 20000, "聊天内容"), "history": []}
                entries = raw.get("history", [])
                if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
                    raise ValueError("invalid history")
                for entry in entries:
                    if not isinstance(entry, dict) or entry.get("kind") not in ("context", "sent"):
                        raise ValueError("invalid history item")
                    profile["history"].append({"kind": entry["kind"], "text": _text(entry.get("text"), 20000, "聊天内容", allow_empty=False)})
                self._trim(profile)
                loaded[name] = profile
            self._profiles = loaded
            self.persistent = True
        except (OSError, ValueError, TypeError, UnicodeError):
            raise ValueError("本地会话记忆文件无法读取，未覆盖原文件。请检查文件或选择新的保存位置。") from None
