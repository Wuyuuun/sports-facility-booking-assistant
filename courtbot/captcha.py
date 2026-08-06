from __future__ import annotations

import logging
import time

log = logging.getLogger("courtbot.captcha")


class CaptchaHandler:
    """腾讯滑块验证码处理。

    mode=manual：脚本让用户在已打开的浏览器里手动拖动滑块，
    随后页面自己会发出 order/add 请求，脚本通过 FlowRecorder 捕获它，
    因此不需要解析验证码本身。

    mode=auto：预留接口（需接入打码服务或自动拖拽，尚未实现）。
    """

    def __init__(self, cfg):
        self.cfg = cfg

    def wait_order_add(self, recorder, timeout: float = 180.0) -> dict:
        log.info("请在弹出的浏览器中拖动滑块完成人机验证（如有）……")
        req = recorder.wait_request(r"/api/booking/order/add", timeout=timeout)
        # 请求刚发出时响应体可能尚未被 CDP 捕获；等它到达再返回，
        # 否则订单号提取会误报“响应不是 JSON”（订单其实已创建）。
        deadline = time.time() + 15
        while not req.get("body") and time.time() < deadline:
            time.sleep(0.2)
            req = recorder.find_response(r"/api/booking/order/add")
        log.info("已捕获 order/add 请求")
        return req

    def solve_for_api(self, place_id: str, day: str, time_key: str):
        """纯接口直连模式下获取滑块票据（未来实现）。"""
        raise NotImplementedError(
            "captcha.mode=auto 尚未实现；当前请使用 manual 模式（浏览器手动拖滑块）"
        )
