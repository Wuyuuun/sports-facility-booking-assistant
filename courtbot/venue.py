from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger("courtbot.venue")

VENUE_API = "https://venue.mo.gov.mo/venue-rental/api/venue-rental/v1.0/app"
# 一户通门户中「場地設施預約」服务的真实入口（来自门户服务列表接口）
VENUE_PORTAL_URL = "https://venue.mo.gov.mo/venue-rental/?hasDept=True"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)


class VenueClient:
    """venue.mo.gov.mo 场馆预约入口 API（来自 HAR 分析）。"""

    def __init__(self, cookies: list | None = None, authorization: str | None = None):
        self.s = requests.Session()
        self.s.headers.update(
            {
                "Origin-Platform": "one-account",
                "Accept-Language": "zh-Hant",
                "User-Agent": UA,
                "Referer": (
                    "https://venue.mo.gov.mo/venue-rental/web-page-home"
                    "?accountType=Personal&hasDept=True&language=zh-Hant"
                ),
            }
        )
        if authorization:
            self.s.headers["Authorization"] = authorization
        for c in cookies or []:
            try:
                self.s.cookies.set(c["name"], c["value"], domain=c.get("domain"))
            except Exception:  # noqa: BLE001
                pass

    def get_booking_uri(self, venue_code: str) -> str:
        """POST applications/{code} 返回带 apicode 的 booking.sport.gov.mo 跳转 URI。"""
        last = None
        for attempt in range(1, 4):
            try:
                return self._get_booking_uri_once(venue_code)
            except requests.ConnectionError as exc:
                last = exc
                log.warning("网络请求失败（第 %d/3 次）: %s", attempt, exc)
                time.sleep(2 * attempt)
        raise last

    def _get_booking_uri_once(self, venue_code: str) -> str:

        home = self.s.get(
            "https://venue.mo.gov.mo/venue-rental/web-page-home"
            "?accountType=Personal&hasDept=True&language=zh-Hant",
            timeout=20,
        )
        log.info("场馆预约首页状态: %s", home.status_code)


        r = self.s.post(
            f"{VENUE_API}/id/applications/{venue_code}",
            json={},
            headers={
                "Accept": "application/json",
                "Accept-Origin": "GOV-WEB",
                "Origin": "https://venue.mo.gov.mo",
                "Content-Type": "application/json;charset=UTF-8",
            },
            timeout=20,
        )
        # 第二步：把服务器返回打出来，失败时能看到真正原因
        log.info("applications 响应状态: %s", r.status_code)
        log.info("applications 响应内容: %s", r.text[:500])

        if r.status_code != 200:
            raise RuntimeError(f"申请接口 HTTP 失败: {r.status_code} {r.text[:500]}")

        body = r.json()
        data = body.get("data")
        if not data or not data.get("uri"):
            raise RuntimeError(f"申请接口未返回 URI: {r.text[:500]}")

        uri = data["uri"]
        log.info("已获取 booking 跳转 URI（含 apicode）")
        return uri
