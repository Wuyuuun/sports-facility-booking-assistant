from __future__ import annotations

import logging
import os
import re
import sqlite3
from dataclasses import dataclass

log = logging.getLogger("courtbot.sms")


@dataclass
class SmsContext:
    order_id: str
    timeout_seconds: int = 120


class ManualSmsProvider:
    """手动输入：验证码同步到 Mac 后在终端输入。"""

    def __init__(self, notifier):
        self.notifier = notifier

    def get_code(self, ctx: SmsContext) -> str:
        self.notifier.notify("验证码已发送", "请查看 Mac 上的短信，然后在终端输入验证码")
        code = input("请输入短信验证码: ").strip()
        return code


class IMessageSmsProvider:
    """读取 Mac「信息」App 最近短信中的 6 位验证码（只读本机数据）。

    前提：
    1. iPhone 已开启「设置 → 信息 → 短信转发」并把 Mac 加进去；
    2. 运行本脚本的终端/应用已获得「完全磁盘访问权限」，
       否则 sqlite3 打不开 ~/Library/Messages/chat.db。
    """

    def __init__(self, sender_pattern: str = "", lookback_minutes: int = 10):
        self.sender_pattern = sender_pattern
        self.lookback_minutes = lookback_minutes

    def get_code(self, ctx: SmsContext) -> str:
        db = os.path.expanduser("~/Library/Messages/chat.db")
        if not os.path.exists(db):
            raise RuntimeError(
                "找不到 ~/Library/Messages/chat.db。请确认 Mac 已登录 iMessage，"
                "并为本终端授予「完全磁盘访问权限」（系统设置 → 隐私与安全性 → 完全磁盘访问权限）。"
            )
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute(
            """
            SELECT m.text, m.date / 1000000000.0 + 978307200, h.id
            FROM message m
            JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.text IS NOT NULL
            ORDER BY m.date DESC
            LIMIT 50
            """
        ).fetchall()
        con.close()

        import time

        cutoff = time.time() - self.lookback_minutes * 60
        for text, ts, sender in rows:
            if ts < cutoff:
                continue
            if self.sender_pattern and self.sender_pattern not in (sender or ""):
                continue
            m = re.search(r"\b(\d{6})\b", text or "")
            if m:
                log.info(
                    "在 %s 的短信中找到验证码",
                    sender,
                )
                return m.group(1)
        raise TimeoutError(
            f"最近 {self.lookback_minutes} 分钟内未找到 6 位验证码"
            + ("（已按 sender_pattern 过滤）" if self.sender_pattern else "")
        )


def make_sms_provider(cfg, notifier):
    if cfg.sms.mode == "imessage":
        return IMessageSmsProvider(sender_pattern=cfg.sms.sender_pattern)
    return ManualSmsProvider(notifier)
