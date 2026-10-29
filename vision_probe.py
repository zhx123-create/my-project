"""Test generation workflows with synthetic inputs, never user chats."""
import secrets

from PIL import Image, ImageDraw, ImageFont

from server import ServiceError, generate, make_messages
from windows_capture import image_payload


def check_text(config):
    messages = make_messages({
        "context": "对方：周五下午三点开会，收到请确认。",
        "intent": "这是虚构的文字生成测试。简短确认收到会议时间，不补充事实；表情建议为空。",
        "reply_count": 1, "include_analysis": True,
    })
    generate(config, messages, reply_count=1, include_analysis=True)
    return True


def check_vision(config):
    # Keep the expected code out of the text prompt. A model which silently
    # loses image input must not pass by merely repeating a supplied answer.
    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
    image = Image.new("RGB", (360, 140), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=56)
    left, top, right, bottom = draw.textbbox((0, 0), code, font=font)
    draw.text(((image.width - right + left) / 2 - left, (image.height - bottom + top) / 2 - top), code, font=font, fill="black")
    messages = make_messages({
        "images": [image_payload(image)], "reply_count": 1,
        "include_analysis": True, "transcribe_images": True,
        "intent": "这是合成识字测试图，不是聊天。请将图中六位字母数字识别码原样放入 screenshot_text，不加前缀或解释。understanding 写识字测试，replies 给一条简短确认；表情建议为空。",
    })
    messages[0]["content"] += "本次为合成识字验证，不要求图片包含真实聊天；请转写图中六位字母数字识别码到 screenshot_text，其他JSON结构不变。不能读图时返回空字符串，不猜测识别码。"
    result = generate(config, messages, reply_count=1, include_analysis=True, transcribe_images=True)
    if "".join(result["screenshot_text"].split()).upper() != code:
        raise ServiceError("图片测试未通过：模型没有正确读出合成测试图。请向服务商确认当前模型与接口的图片输入支持；这不能仅凭模型列表判断。")
    return True
