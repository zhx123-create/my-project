"""Runtime-only scheduling and deduplication for an explicitly selected chat.

This module performs no I/O. Snapshot overlap is a continuity heuristic, not
proof of a contact's identity: another conversation in the same native window
cannot reliably be identified from message text alone.
"""
from collections import deque
from dataclasses import dataclass
import hashlib
import math
import re


ALLOWED_INTERVALS = (5, 10, 20, 30)
MAX_SNAPSHOT_CHARS = 12000
_READER_HEADER = "【微信可访问消息文字；发言者及会话身份未自动核实，请核对】"
_MAX_SEEN = 128


@dataclass(frozen=True)
class WindowBinding:
    handle: int
    pid: int
    title: str


def normalize_snapshot(text):
    """Remove only the known reader note and incidental empty/outer whitespace."""
    if not isinstance(text, str) or len(text) > MAX_SNAPSHOT_CHARS:
        raise ValueError("自动检查的聊天文字必须是最多 12000 字的文字。")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    if lines and lines[0].strip() == _READER_HEADER:
        lines = lines[1:]
    normalized = "\n".join(line.strip() for line in lines if line.strip())
    if not normalized:
        raise ValueError("没有可用于自动检查的消息文字。")
    return normalized


def _time_value(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("自动检查需要有效的单调时间。")
    return float(value)


def _binding(window):
    try:
        handle, pid, title = window.handle, window.pid, window.title
    except AttributeError:
        raise ValueError("请先选择有效的微信窗口。") from None
    if type(handle) is not int or type(pid) is not int or handle <= 0 or pid <= 0 or not isinstance(title, str) or len(title) > 1000:
        raise ValueError("请先选择有效的微信窗口。")
    return WindowBinding(handle, pid, title)


def _fingerprint(text):
    return hashlib.sha256(text.encode("utf-8")).digest()


def _contains_lines(haystack, needle):
    if not needle or len(needle) > len(haystack):
        return False
    # Delimit both ends to match complete lines, while letting Python's bounded
    # string search avoid repeated large list slices on long multiline messages.
    return "\n" + "\n".join(needle) + "\n" in "\n" + "\n".join(haystack) + "\n"


def _meaningful_lines(lines):
    # Time separators do not establish continuity. Ordinary short messages may
    # overlap, but that still does not establish who the current contact is.
    return {line for line in lines if not re.fullmatch(r"[\d\s:：./年月日星期周一二三四五六七天上午下午晚上昨天今天-]+", line)}


class ConversationWatch:
    """Keep reads serialized, deduplicate snapshots and preserve pending text.

    ``due`` has no side effects. Call ``mark_check`` before starting a worker,
    and capture ``generation`` with that worker to reject results after a stop
    or restart. Root UI code owns the decision to consume ``pending_text``;
    merely observing another snapshot does not imply that a reply was sent.
    """

    def __init__(self, interval=10):
        self.interval = interval
        self.active = False
        self.reason = ""
        self.profile = None
        self.window = None
        self.generation = 0
        self.next_check_at = None
        self.pending_text = ""
        self.acknowledged_text = ""
        self._previous = ""
        self._seen = set()
        self._seen_order = deque()

    @property
    def interval(self):
        return self._interval

    @interval.setter
    def interval(self, value):
        if type(value) is not int or value not in ALLOWED_INTERVALS:
            raise ValueError("自动检查间隔请选择 5、10、20 或 30 秒。")
        self._interval = value

    def start(self, profile, window, baseline_text="", now=None):
        if not isinstance(profile, str) or not profile.strip() or len(profile) > 100:
            raise ValueError("请先选择一个已命名的会话。")
        selected = _binding(window)
        if not isinstance(baseline_text, str):
            raise ValueError("会话基线必须是文字。")
        baseline = normalize_snapshot(baseline_text) if baseline_text.strip() else ""
        timestamp = _time_value(now) if now is not None else None
        self.generation += 1
        self.active, self.reason = True, ""
        self.profile, self.window = profile.strip(), selected
        self.next_check_at = timestamp
        self.pending_text = self.acknowledged_text = ""
        self._previous = baseline
        self._seen.clear()
        self._seen_order.clear()
        if baseline:
            self._remember(baseline)

    def stop(self, reason=""):
        if not isinstance(reason, str):
            raise ValueError("暂停原因必须是文字。")
        self.generation += 1
        self.active = False
        self.reason = reason
        self.next_check_at = None
        # Keep pending text available for the UI to show or manually analyze.

    def matches_window(self, window):
        try:
            return self.window is not None and self.window == _binding(window)
        except ValueError:
            return False

    def due(self, now, busy=False):
        timestamp = _time_value(now)
        return self.active and not busy and (self.next_check_at is None or timestamp >= self.next_check_at)

    def mark_check(self, now):
        timestamp = _time_value(now)
        if self.active:
            self.next_check_at = timestamp + self.interval

    def _remember(self, text):
        digest = _fingerprint(text)
        if digest not in self._seen:
            self._seen.add(digest)
            self._seen_order.append(digest)
            if len(self._seen_order) > _MAX_SEEN:
                self._seen.discard(self._seen_order.popleft())

    def accept_snapshot(self, text, now=None):
        """Return changed/same/discontinuous, never perform an AI request.

        A smaller or older viewport is not new content. A completely unrelated
        snapshot pauses the watcher before the caller can merge it into memory.
        This deliberately errs toward pausing when messages cease to overlap.
        """
        if not self.active:
            return "same"
        normalized = normalize_snapshot(text)
        if now is not None:
            self.mark_check(now)
        previous = self._previous
        old_lines, new_lines = previous.splitlines(), normalized.splitlines()
        seen = _fingerprint(normalized) in self._seen
        if previous and normalized != previous:
            if _contains_lines(old_lines, new_lines):
                self._previous = normalized
                self._remember(normalized)
                return "same"
            if not (_meaningful_lines(old_lines) & _meaningful_lines(new_lines)):
                self.stop("消息上下文不连续，已暂停自动辅助。请核对微信当前聊天后重新开启。")
                return "discontinuous"
            # Expanding upward to older history, with the same visible tail,
            # is a viewport change rather than a new incoming message.
            if len(new_lines) >= len(old_lines) and new_lines[-len(old_lines):] == old_lines:
                self._previous = normalized
                self._remember(normalized)
                return "same"
        self._previous = normalized
        self._remember(normalized)
        if seen:
            return "same"
        self.pending_text = normalized
        return "changed"

    def acknowledge(self, text):
        """Mark a snapshot successfully handled, independently of observed text."""
        normalized = normalize_snapshot(text)
        self.acknowledged_text = normalized
        self._remember(normalized)
        if self.pending_text == normalized:
            self.pending_text = ""
