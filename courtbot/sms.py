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
    newer_than: float = 0.0  # 只接受晚于该时间戳的短信（发送验证码前记录的基线）


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

    def newest_ts(self) -> float:
        """iMessage 中最新一条短信的时间戳；发送验证码前调用，作为「只读新短信」基线。"""
        db = os.path.expanduser("~/Library/Messages/chat.db")
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            ts = con.execute(
                "SELECT MAX(m.date / 1000000000.0 + 978307200) "
                "FROM message m WHERE m.text IS NOT NULL"
            ).fetchone()[0]
            con.close()
            return float(ts or 0)
        except Exception:  # noqa: BLE001
            return 0.0

    def get_code(self, ctx: SmsContext) -> str:
        db = os.path.expanduser("~/Library/Messages/chat.db")
        if not os.path.exists(db):
            raise RuntimeError(
                "找不到 ~/Library/Messages/chat.db。请确认 Mac 已登录 iMessage，"
                "并为本终端授予「完全磁盘访问权限」（系统设置 → 隐私与安全性 → 完全磁盘访问权限）。"
            )
        import time

        # 短信从「发送」到「同步到 Mac」有几秒延迟，需轮询等待而不是只查一次
        cutoff_newer = getattr(ctx, "newer_than", 0.0) or 0.0
        deadline = time.time() + ctx.timeout_seconds
        while time.time() < deadline:
            try:
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
            except Exception as exc:  # noqa: BLE001
                log.warning("读取 iMessage 数据库失败（重试中）: %s", exc)
                rows = []

            cutoff = time.time() - self.lookback_minutes * 60
            for text, ts, sender in rows:
                if ts <= cutoff_newer:
                    # 只接受发送验证码之后到达的新短信，避免读到上一次的旧码
                    continue
                if ts < cutoff:
                    continue
                if self.sender_pattern and self.sender_pattern not in (sender or ""):
                    continue
                m = re.search(r"\b(\d{6})\b", text or "")
                if m:
                    log.info("在 %s 的短信中找到验证码", sender)
                    return m.group(1)
            time.sleep(2)
        raise TimeoutError(
            f"{ctx.timeout_seconds} 秒内未在 iMessage 中找到 6 位验证码"
            + ("（已按 sender_pattern 过滤）" if self.sender_pattern else "")
        )


def make_sms_provider(cfg, notifier):
    if cfg.sms.mode == "imessage":
        return IMessageSmsProvider(sender_pattern=cfg.sms.sender_pattern)
    return ManualSmsProvider(notifier)
