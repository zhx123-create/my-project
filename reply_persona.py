"""Bounded, user-authored speaking preferences, independent of chat facts."""
DEFAULT_PERSONA = {"name": "自然的我", "description": "", "examples": ""}


def validate_persona(value=None):
    if value is None:
        return dict(DEFAULT_PERSONA)
    if not isinstance(value, dict) or set(value) - set(DEFAULT_PERSONA):
        raise ValueError("人设包含不支持的字段。")
    result = {**DEFAULT_PERSONA, **value}
    for field, limit in (("name", 60), ("description", 1000), ("examples", 3000)):
        text = result[field]
        if not isinstance(text, str) or len(text) > limit or "\x00" in text:
            raise ValueError(f"人设的 {field} 必须是最多 {limit} 字的文字。")
        result[field] = text.strip()
    if not result["name"]:
        result["name"] = DEFAULT_PERSONA["name"]
    return result
