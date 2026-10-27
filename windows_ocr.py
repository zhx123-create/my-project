"""Local Windows OCR: images stay in RAM and are passed over an anonymous pipe."""
import base64
import io
import json
from pathlib import Path
import subprocess
import sys

from PIL import Image
from windows_capture import CaptureError

OCR_HEADER = "【本机截图识别；发言者及会话身份未自动核实，请核对】\n"
_ERRORS = {
    "runtime": "Windows 系统识字组件未能启动。请检查系统 OCR 与语言功能，或先粘贴聊天文字继续。",
    "decode": "Windows 未能解码这张截图。请重新截图，或打开正常的 PNG/JPEG 文件后重试。",
    "language": "Windows 未提供可用的文字识别语言。请在系统语言选项中安装中文的“光学字符识别”功能，或先粘贴聊天文字。",
    "failed": "Windows 本机识字失败。请确认 Windows 10/11 的文字识别组件可用，也可先粘贴聊天文字。",
}


def recognize_image(image, timeout=15):
    if sys.platform != "win32":
        raise CaptureError("本机截图识字仅支持 Windows 10/11。")
    if not isinstance(image, Image.Image) or min(image.size) < 20 or image.width * image.height > 40_000_000:
        raise CaptureError("截图无效或过小，请重新选择聊天消息区域。")
    prepared = image.convert("RGB")
    prepared.thumbnail((2000, 2000), Image.Resampling.LANCZOS)
    stream = io.BytesIO(); prepared.save(stream, format="PNG")
    if stream.tell() > 12 * 1024 * 1024:
        raise CaptureError("截图过大，请缩小到近期聊天消息区域。")
    try:
        script = Path(__file__).with_suffix(".ps1").read_text(encoding="utf-8")
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        response = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], input=base64.b64encode(stream.getvalue()), capture_output=True, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000), check=False)
    except subprocess.TimeoutExpired:
        raise CaptureError("本机截图识字超时，已停止本次处理。") from None
    except OSError:
        raise CaptureError("未能启动 Windows 本机识字，请确认系统 PowerShell 可用。") from None
    if response.returncode:
        try:
            code = json.loads(response.stderr).get("ocr_error", "failed")
        except (ValueError, TypeError, AttributeError):
            code = "failed"
        raise CaptureError(_ERRORS.get(code, _ERRORS["failed"]))
    try:
        if len(response.stdout) > 160000:
            raise ValueError("oversized OCR")
        data = json.loads(response.stdout)
        text = data.get("text") if isinstance(data, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise CaptureError("截图中没有识别到文字。请放大微信字体、露出近期消息，并排除空白或纯图片区域。")
        text = text.replace("\x00", "").strip()
        text = text[-(12000 - len(OCR_HEADER)):]
        count = min(100, max(1, len(text.splitlines())))
        return {"text": OCR_HEADER + text, "count": count, "source": "windows_ocr", "identity_verified": False}
    except (ValueError, TypeError, UnicodeError):
        raise CaptureError(_ERRORS["failed"]) from None
