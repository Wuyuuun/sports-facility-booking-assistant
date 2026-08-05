from __future__ import annotations

import logging
import time
from pathlib import Path

import requests

log = logging.getLogger("courtbot.booking_api")

BASE = "https://booking.sport.gov.mo/api"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)


class BookingClient:
    """booking.sport.gov.mo 预约 API 客户端（请求头结构来自 HAR）。"""

    def __init__(self, api_key: str, response_dir: Path | None = None):
        self.response_dir = response_dir
        self.s = requests.Session()
        self.s.headers.update(
            {
                "api-key": api_key,
                "api-language": "zh-MO",
                "Accept": "application/json",
                "Origin": "https://booking.sport.gov.mo",
                "Referer": "https://booking.sport.gov.mo/zh/booking/time",
                "User-Agent": UA,
            }
        )

    def _call(
        self, method: str, path: str, *, params=None, json_body=None, name: str = ""
    ) -> dict:
        last = None
        for attempt in range(1, 4):
            try:
                return self._call_once(method, path, params=params, json_body=json_body, name=name)
            except requests.ConnectionError as exc:
                last = exc
                log.warning("网络请求失败（第 %d/3 次）: %s", attempt, exc)
                time.sleep(2 * attempt)
        raise last

    def _call_once(
        self, method: str, path: str, *, params=None, json_body=None, name: str = ""
    ) -> dict:
        url = BASE + path
        r = self.s.request(method, url, params=params, json=json_body, timeout=20)
        r.raise_for_status()
        try:
            body = r.json()
        except ValueError:
            body = {"_raw": r.text}
        if self.response_dir:
            ts = time.strftime("%Y%m%d-%H%M%S")
            out = self.response_dir / f"{ts}-{name or method}.json"
            out.write_text(r.text, encoding="utf-8")
            log.info("响应已保存: %s", out)
        return body

    def open_time(self, place_id: str, day: str, only_book: bool = False) -> dict:
        """查询指定场地某天各时段余量。"""
        return self._call(
            "GET",
            "/booking/place/open_time",
            params={
                "webview": 0,
                "placeId": place_id,
                "onlyBook": str(only_book).lower(),
                "day": day,
            },
            name="open_time",
        )

    def place_list(self, venue_id: str, sport_id: str) -> dict:
        """查询场馆下的场地列表（用于确认三号场 placeId）。"""
        return self._call(
            "GET",
            "/booking/place/list",
            params={"webview": 0, "venueId": venue_id, "sportId": sport_id},
            name="place_list",
        )

    def setting_init(self) -> dict:
        """预约系统基础配置（可预约日期范围 dayRanges 等）。"""
        return self._call("GET", "/setting/init", name="setting_init")

    def order_start(self, place_id: str, day: str, time_key: str) -> dict:
        """下单第一步（无验证码），用于占位/校验。"""
        return self._call(
            "POST",
            "/booking/order/start",
            params={"webview": 0},
            json_body={"placeId": place_id, "day": day, "timeKey": time_key},
            name="order_start",
        )

    def order_add(
        self, place_id: str, day: str, time_key: str, randstr: str, recaptcha: str
    ) -> dict:
        """下单第二步，需要腾讯滑块验证码票据（randstr + recaptcha）。"""
        return self._call(
            "POST",
            "/booking/order/add",
            params={"webview": 0},
            json_body={
                "placeId": place_id,
                "day": day,
                "timeKey": time_key,
                "randstr": randstr,
                "recaptcha": recaptcha,
            },
            name="order_add",
        )

    def payment_info(self, order_id: str) -> dict:
        return self._call(
            "GET",
            f"/booking/payment/info/{order_id}",
            params={"webview": 0},
            name="payment_info",
        )

    def send_captcha(self, order_id: str) -> dict:
        """触发发送短信验证码（POST 无请求体）。"""
        return self._call(
            "POST",
            f"/booking/payment/send_captcha/{order_id}",
            params={"webview": 0},
            name="send_captcha",
        )

    def payment_start(self, order_id: str, request_id: str, way: str, code: str) -> dict:
        """提交短信验证码并开始支付，返回支付跳转信息。"""
        return self._call(
            "POST",
            f"/booking/payment/start/{order_id}",
            params={"webview": 0},
            json_body={"requestId": request_id, "way": way, "code": code},
            name="payment_start",
        )
