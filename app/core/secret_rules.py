"""SECRET_KEY 合格性规则。单独成模块、无副作用: app.core.config 在导入时就会创建 settings(密钥不合格会直接抛错),
而初始化向导要在"密钥还不存在"时判断 .env 里现有的值能不能用, 所以规则不能放在 config.py 里。"""
from __future__ import annotations

from typing import Optional

MIN_SECRET_LEN = 32
_PLACEHOLDER_HINTS = ("change-me", "changeme", "change_me", "your-secret", "your_secret", "example", "placeholder", "please-change")
_WEAK_SECRETS = {"secret", "password", "default", "secretkey", "secret_key", "admin", "aims", "test", "dev"}


def secret_problem(key: Optional[str]) -> Optional[str]:
    """None = 合格; 否则返回原因。"""
    k = (key or "").strip()
    low = k.lower()
    if not k:
        return "为空"
    # 先判占位符再判长度: 复制 .env.example 忘了改的人, "是占位符"比"太短"更能指出该做什么
    if low in _WEAK_SECRETS or any(h in low for h in _PLACEHOLDER_HINTS):
        return "是占位符或常见弱值"
    if len(k) < MIN_SECRET_LEN:
        return f"太短(至少 {MIN_SECRET_LEN} 个字符)"
    if len(set(k)) < 8:
        return "字符过于单一"
    return None


def is_acceptable_secret(key: Optional[str]) -> bool:
    return secret_problem(key) is None
