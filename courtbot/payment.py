from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger("courtbot.payment")

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)


def find_payment_url(data) -> str:
    """在 payment/start 的响应里递归寻找 aas.bocmacau.com 的支付 URL。"""

    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                hit = walk(v)
                if hit:
                    return hit
        elif isinstance(node, list):
            for v in node:
                hit = walk(v)
                if hit:
                    return hit
        elif isinstance(node, str) and "aas.bocmacau.com" in node:
            return node
        return None

    return walk(data)


class BOCPaymentClient:
    """中银智慧付（BOC）/ MPay 扫码支付：initOrder + 状态轮询。"""

    def __init__(self, poll_interval: float = 2.0):
        self.poll_interval = poll_interval
        self.s = requests.Session()
        self.s.headers.update(
            {
                "Referer": "https://aas.bocmacau.com/pcweb/index.html",
                "User-Agent": UA,
            }
        )

    def init_order(self, token: str) -> dict:
        r = self.s.post(
            "https://aas.bocmacau.com/w/initOrder.do",
            data={"token": token, "trans_way": "pc", "nowcode": ""},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()

    def get_order_status(self, token: str) -> dict:
        r = self.s.post(
            "https://aas.bocmacau.com/w/getOrderStatus.do",
            data={"token": token, "nowcode": ""},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()

    def wait_paid(self, token: str, timeout_seconds: float = 600.0) -> dict:
        """轮询支付状态。HAR 中未支付时 ord_sts='U'；已支付状态值待集成时确认。"""
        deadline = time.time() + timeout_seconds
        last = {}
        while time.time() < deadline:
            last = self.get_order_status(token)
            sts = last.get("ord_sts", "")
            log.info("支付状态 ord_sts=%s", sts)
            if sts and sts != "U":
                return last
            time.sleep(self.poll_interval)
        return last
