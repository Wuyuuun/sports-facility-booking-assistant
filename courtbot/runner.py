from __future__ import annotations

import logging
import json
import os
import re
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import requests
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from courtbot import login, venue
from courtbot.booking_api import BookingClient
from courtbot.browser import BrowserManager, FlowRecorder
from courtbot.captcha import CaptchaHandler
from courtbot.notifier import Notifier
from courtbot.payment import find_payment_url
from courtbot.selection import Selection, SelectionStore
from courtbot.session import Session, SessionStore
from courtbot.sms import IMessageSmsProvider, SmsContext, make_sms_provider

log = logging.getLogger("courtbot.runner")


class Runner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.store = SessionStore(cfg.state_dir / "session.json")
        self.selection_store = SelectionStore(cfg.state_dir / "selection.json")
        self.notifier = Notifier(cfg.notify)
        self.sms_provider = make_sms_provider(cfg, self.notifier)

    # ---------- 会话 ----------
    def _capture_venue_token(self, driver, recorder: FlowRecorder) -> str:
        """打开场馆预约入口，让 SPA 自动完成 OAuth 换令牌，
        再从网络记录中抓取 token/oauth2 响应里的 venue JWT。"""
        log.info("打开场馆预约入口: %s", venue.VENUE_PORTAL_URL)
        driver.get(venue.VENUE_PORTAL_URL)

        token = ""
        deadline = time.time() + 60
        while time.time() < deadline:
            req = recorder.find_response(r"/token/oauth2")
            if req.get("body"):
                try:
                    token = (json.loads(req["body"]).get("data") or "").strip()
                except Exception:  # noqa: BLE001
                    token = ""
                if token:
                    break
            time.sleep(0.5)

        if not token:
            raise RuntimeError(
                "未捕获到 venue 令牌（token/oauth2 响应为空）。"
                "可能需要在浏览器里手动完成一次登录跳转，请重试并观察浏览器"
            )
        log.info("已获取 venue JWT（%d 字符）", len(token))
        return token

    def ensure_session(self, driver, recorder: FlowRecorder, force: bool = False) -> Session:
        s = self.store.load()
        if not force and s.is_valid():
            log.info("复用已保存会话")
            return s

        log.info("会话缺失或过期，重新建立")
        login.login(driver, self.cfg)
        s.cookies = driver.get_cookies()
        s.venue_token = self._capture_venue_token(driver, recorder)

        vc = venue.VenueClient(cookies=s.cookies, authorization=s.venue_token)
        uri = vc.get_booking_uri(self.cfg.venue.venue_code)
        s.booking_uri = uri

        driver.get(uri)
        s.api_key = self._capture_valid_api_key(recorder)
        s.fetched_at = time.time()
        self.store.save(s)
        log.info("会话建立完成")
        return s

    def _capture_valid_api_key(self, recorder: FlowRecorder, attempts: int = 4) -> str:
        """捕获并实测校验 booking api-key。

        优先等新预约页面加载后才会发出的 place/list 请求的 key（对应新 apicode），
        避免先抓到旧页面残留的 key 导致 401 重试（实测可省 5-7 秒）。
        """
        last_err: Exception | None = None
        key = recorder.wait_for_api_key(r"/api/booking/place/list", timeout=6)
        for i in range(attempts):
            if not key:
                if i:
                    time.sleep(2)
                key = recorder.find_api_key()
            if not key:
                last_err = RuntimeError(
                    "未捕获到 booking.sport.gov.mo 的 api-key 请求头。"
                    "请先运行 `python main.py discover` 确认 security 页面加载流程"
                )
                continue
            probe = BookingClient(key)
            try:
                probe.place_list(
                    self.cfg.venue.booking_venue_id, self.cfg.venue.booking_sport_id
                )
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 401:
                    log.warning(
                        "api-key 返回 401（可能是旧页面残留），等待刷新后重取（%d/%d）",
                        i + 1,
                        attempts,
                    )
                    last_err = exc
                    key = ""
                    continue
                raise
            log.info("api-key 校验通过（第 %d 次尝试）", i + 1)
            return key
        if isinstance(last_err, requests.HTTPError):
            raise RuntimeError("未能捕获可用的 booking api-key（多次 401）")
        raise last_err or RuntimeError("未捕获到 booking api-key")

    def _ensure_ready(self, bm: BrowserManager, recorder: FlowRecorder) -> Session:
        """返回有效会话，并保证浏览器已打开一个刚生成的 booking 页面。

        booking 授权链接里的 apicode 是一次性的，不能跨运行复用；
        因此每次运行都要重新申请并打开，避免页面显示“授权失败请重试”。
        """
        s = self.store.load()
        if s.is_valid():
            try:
                vc = venue.VenueClient(cookies=s.cookies, authorization=s.venue_token)
                s.booking_uri = vc.get_booking_uri(self.cfg.venue.venue_code)
                log.info("已刷新一次性 booking 授权链接")
            except Exception as exc:  # noqa: BLE001
                log.warning("刷新 booking 授权链接失败（%s），强制重建会话", exc)
                s = self.ensure_session(bm.driver, recorder, force=True)
                self.store.save(s)
                return s

            recorder.start()
            bm.driver.get(s.booking_uri)
            s.api_key = self._capture_valid_api_key(recorder, attempts=3)
            s.fetched_at = time.time()
            self.store.save(s)
            return s

        s = self.ensure_session(bm.driver, recorder)
        self.store.save(s)
        return s

    # ---------- 时间 ----------
    def _tz(self) -> ZoneInfo:
        return ZoneInfo(self.cfg.timezone)

    def next_release(self) -> datetime:
        hh, mm, ss = (int(x) for x in self.cfg.booking.release_time.split(":"))
        now = datetime.now(self._tz())
        release = datetime.combine(now.date(), dtime(hh, mm, ss), tzinfo=self._tz())
        if release <= now:
            release += timedelta(days=1)
        return release

    def target_day(self, release: datetime | None = None) -> str:
        release = release or self.next_release()
        day = release.date() + timedelta(days=self.cfg.booking.offset_days)
        if self.cfg.booking.day_of_week >= 0 and day.weekday() != self.cfg.booking.day_of_week:
            log.warning("目标日期 %s 不是设定的星期 %s", day, self.cfg.booking.day_of_week)
        log.info("目标日期: %s（%s）", day, day.strftime("%A"))
        return day.isoformat()

    def _wait_until_release(self, day: str | None = None) -> bool:
        """等待放场时间；返回是否真的等过（False=放场时间已过，直接开抢）。"""
        if day:
            tz = self._tz()
            hh, mm, ss = (int(x) for x in self.cfg.booking.release_time.split(":"))
            target = datetime.strptime(day, "%Y-%m-%d").date()
            release = datetime.combine(
                target - timedelta(days=self.cfg.booking.offset_days),
                dtime(hh, mm, ss),
                tzinfo=tz,
            )
            if release <= datetime.now(tz):
                log.warning("目标日期 %s 的放场时间已过，直接尝试抢场", day)
                return False
            log.info("放场时间: %s", release.isoformat())
            while True:
                now = datetime.now(tz)
                if now >= release:
                    log.info("放场时间到")
                    return True
                delta = (release - now).total_seconds()
                if delta > 30:
                    time.sleep(5)
                elif delta > 1:
                    time.sleep(0.05)
                else:
                    time.sleep(0.005)
            return

        release = self.next_release()
        log.info("下一次放场时间: %s", release.isoformat())
        while True:
            now = datetime.now(self._tz())
            if now >= release:
                log.info("放场时间到")
                return True
            delta = (release - now).total_seconds()
            if delta > 30:
                time.sleep(5)
            elif delta > 1:
                time.sleep(0.05)
            else:
                time.sleep(0.005)

    # ---------- 查询 ----------
    def check(self, day: str | None = None) -> dict:
        sel = self.selection_store.load()
        place_id = sel.place_id or self.cfg.venue.place_id
        s = self.store.load()
        if s.is_valid():
            log.info("复用已保存会话")
        else:
            with BrowserManager(self.cfg) as bm:
                recorder = bm.recorder()
                s = self.ensure_session(bm.driver, recorder)
        client = BookingClient(
            s.api_key, response_dir=self.cfg.state_dir / "responses"
        )
        day = day or sel.day or self.target_day()
        try:
            body = client.open_time(place_id, day)
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 401:
                raise
            log.warning("open_time 请求 401（会话已失效），强制重建会话后重试")
            with BrowserManager(self.cfg) as bm:
                recorder = bm.recorder()
                s = self.ensure_session(bm.driver, recorder, force=True)
            client = BookingClient(
                s.api_key, response_dir=self.cfg.state_dir / "responses"
            )
            body = client.open_time(place_id, day)
        self._print_availability(body)
        return body

    def options(self, day: str | None = None) -> dict:
        """输出 JSON 选项列表（供 GUI 使用）：日期范围、场地、时段、当前选择。"""
        sel = self.selection_store.load()
        for attempt in range(2):
            s = self.store.load()
            if not s.is_valid():
                with BrowserManager(self.cfg) as bm:
                    recorder = bm.recorder()
                    s = self.ensure_session(bm.driver, recorder, force=(attempt > 0))
            client = BookingClient(s.api_key, response_dir=self.cfg.state_dir / "responses")
            try:
                places = (
                    (client.place_list(
                        self.cfg.venue.booking_venue_id, self.cfg.venue.booking_sport_id
                    ).get("data") or {}).get("placeList") or []
                )
                day_ranges = [
                    d.get("day")
                    for d in ((client.setting_init().get("data") or {}).get("dayRanges") or [])
                ]
                # 可预约范围外追加未来几天（供「预定」尚未放场的日期）
                if day_ranges:
                    last_day = datetime.strptime(day_ranges[-1], "%Y-%m-%d").date()
                    for i in range(1, int(self.cfg.booking.extra_days) + 1):
                        day_ranges.append((last_day + timedelta(days=i)).isoformat())
                day = day or sel.day or (day_ranges[0] if day_ranges else self.target_day())
                times = (
                    (client.open_time(
                        sel.place_id or self.cfg.venue.place_id, day
                    ).get("data") or {}).get("openTimes") or []
                )
                times = [self._normalize_time(t) for t in times]
                break
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 401 and attempt == 0:
                    log.warning("options 请求 401（会话失效），强制重建会话后重试")
                    with BrowserManager(self.cfg) as bm:
                        recorder = bm.recorder()
                        self.ensure_session(bm.driver, recorder, force=True)
                    continue
                raise
        else:
            raise RuntimeError("options 失败：会话重建后仍 401")
        out = {
            "dayRanges": day_ranges,
            "places": [
                {"id": p.get("id"), "title": p.get("title")} for p in places
            ],
            "times": [
                {
                    "timeKey": t.get("timeKey"),
                    "timeFrom": t.get("timeFrom"),
                    "timeTo": t.get("timeTo"),
                    "statusName": t.get("statusName"),
                    "isCanBook": t.get("isCanBook"),
                }
                for t in times
            ],
            "selection": {
                "place_id": sel.place_id,
                "place_name": sel.place_name,
                "day": sel.day,
                "time_key": sel.time_key,
                "time_label": sel.time_label,
            },
        }
        print(json.dumps(out, ensure_ascii=False))
        return out

    @staticmethod
    def _normalize_time(t: dict) -> dict:
        """站点偶发时段显示错位（如 timeKey=1600 却显示 15:00-17:00），
        以 timeKey 为准修正展示的 timeFrom/timeTo。"""
        key = str(t.get("timeKey") or "")
        if len(key) == 4 and key.isdigit():
            hh = int(key[:2])
            if t.get("timeFrom") != f"{hh:02d}:00":
                t = dict(t)
                t["timeFrom"] = f"{hh:02d}:00"
                t["timeTo"] = f"{hh + 1:02d}:00"
        return t

    def cancel_order(self, order_id: str | None = None) -> None:
        """取消待付款订单；不传订单号则自动取消当前 Lock 的订单。"""
        for attempt in range(2):
            s = self.store.load()
            if not s.is_valid():
                with BrowserManager(self.cfg) as bm:
                    recorder = bm.recorder()
                    s = self.ensure_session(bm.driver, recorder, force=(attempt > 0))
            client = BookingClient(s.api_key, response_dir=self.cfg.state_dir / "responses")
            try:
                target = order_id
                if not target:
                    data = client.check_status().get("data") or {}
                    target = (data.get("number") or "").strip()
                    if not target:
                        log.info("当前没有待处理订单，无需取消")
                        return
                    log.info("检测到待处理订单: %s（status=%s）", target, data.get("status"))
                resp = client.payment_cancel(target)
                if resp.get("code") != 0:
                    raise RuntimeError(
                        f"取消订单失败（{target}）: {resp.get('message') or resp}"
                    )
                log.info("订单已取消: %s（%s）", target, resp.get("message"))
                self.notifier.notify("订单已取消", f"{target} 已取消，场地锁定已解除")
                return
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 401 and attempt == 0:
                    log.warning("请求 401（会话失效），强制重建会话后重试")
                    continue
                raise
        raise RuntimeError("取消订单失败：会话重建后仍 401")

    def _print_availability(self, body: dict) -> None:
        data = body.get("data") or {}
        place = (data.get("place") or {}).get("title", "?")
        day = data.get("day", "?")
        log.info("===== %s %s 余量 =====", day, place)
        times = data.get("openTimes") or []
        if not times:
            log.info("（无场次数据）")
            return
        for t in sorted(times, key=lambda x: x.get("timeKey", "")):
            t = self._normalize_time(t)
            log.info(
                "%s-%s  status=%-8s canBook=%s  price=%s",
                t.get("timeFrom"),
                t.get("timeTo"),
                t.get("statusName") or t.get("status"),
                t.get("isCanBook"),
                t.get("price"),
            )

    # ---------- 抢场 ----------
    def book(self, dry_run: bool = False, rehearsal: bool = False, day: str | None = None) -> None:
        sel = self.selection_store.load()
        place_id = sel.place_id or self.cfg.venue.place_id
        place_name = sel.place_name or self.cfg.venue.name
        time_key = sel.time_key or self.cfg.booking.time_key
        time_label = sel.time_label or self.cfg.booking.time_label
        day = day or sel.day or self.target_day()
        if sel.day:
            log.info("使用已保存选择: %s %s %s", place_name, day, time_label)

        with BrowserManager(self.cfg) as bm:
            recorder = bm.recorder()
            s = self._ensure_ready(bm, recorder)
            client = BookingClient(
                s.api_key, response_dir=self.cfg.state_dir / "responses"
            )

            waited = self._wait_until_release(day)

            if dry_run:
                log.info("dry-run：仅查询余量，不下单")
                try:
                    body = client.open_time(place_id, day)
                except requests.HTTPError as exc:
                    if exc.response is not None and exc.response.status_code == 401:
                        log.warning("open_time 401（api-key 失效），强制重建会话后重试")
                        s = self.ensure_session(bm.driver, recorder, force=True)
                        client = BookingClient(
                            s.api_key, response_dir=self.cfg.state_dir / "responses"
                        )
                        body = client.open_time(place_id, day)
                    else:
                        raise
                self._print_availability(body)
                return

            # 提前预热页面等放场时：放场瞬间刷新页面，让目标日期进入可选范围
            if waited:
                self._refresh_booking_page(bm.driver, recorder, s, place_name, day)

            order_id = self._book_via_ui(
                bm.driver, recorder, day, place_name, time_label, submit=not rehearsal
            )
            if rehearsal:
                log.info("演练结束：日期/场地/时段/条款已选中，未创建订单")
                return
            log.info("订单号: %s", order_id)
            self.notifier.notify("抢场成功", f"订单 {order_id} 已创建，进入支付环节")
            self._payment_flow(client, bm.driver, order_id)

    def _refresh_booking_page(
        self, driver, recorder: FlowRecorder, s: Session, place_name: str, day: str
    ) -> None:
        """放场瞬间刷新预约页，使目标日期进入可选范围；失败则重新申请授权链接。

        提前加载的页面 dayRanges 不含放场时新增的目标日期，必须刷新一次；
        服务器放场可能有秒级延迟，目标日期没出现就重试刷新。
        """
        deadline = time.time() + 15
        refreshed = False
        while time.time() < deadline:
            try:
                recorder.start()
                driver.refresh()
                self._wait_for_booking_widget(driver, place_name, timeout=8)
            except Exception as exc:  # noqa: BLE001
                log.warning("刷新后预约页未就绪（%s），重新申请授权链接", exc)
                vc = venue.VenueClient(cookies=s.cookies, authorization=s.venue_token)
                s.booking_uri = vc.get_booking_uri(self.cfg.venue.venue_code)
                driver.get(s.booking_uri)
                self._wait_for_booking_widget(driver, place_name, timeout=15)
            try:
                driver.find_element(
                    By.XPATH,
                    f"//*[contains(concat(' ', normalize-space(@class), ' '), ' date___3gbyJ ')"
                    f" and normalize-space(text())='{day[5:]}']",
                )
                refreshed = True
                break
            except Exception:  # noqa: BLE001
                log.info("目标日期 %s 尚未出现在日期栏，稍后重试刷新", day)
                time.sleep(1.5)
        if not refreshed:
            log.warning("15 秒内目标日期未出现，继续尝试抢场（_click_date 仍会等待）")
        # 刷新后 SPA 可能换了 api-key，重新捕获
        s.api_key = self._capture_valid_api_key(recorder, attempts=3)
        s.fetched_at = time.time()
        self.store.save(s)

    def _book_via_ui(
        self,
        driver,
        recorder: FlowRecorder,
        day: str,
        place_name: str,
        time_label: str,
        submit: bool = True,
    ) -> str:
        """通过浏览器 UI 完成：选日期/时间 → 提交 → 手动滑块 → 捕获 order/add。

        需要先在 config.yaml 的 selectors 中填好页面选择器（见 README 集成清单）。
        """
        if self._has_pending_payment(driver):
            raise RuntimeError(
                "检测到账户存在未付款订单（页面显示『確認付款及保留場地』）。"
                "请先手动完成支付或取消该订单后重试。"
            )

        self._wait_for_booking_widget(driver, place_name)
        sel = self.cfg.selectors
        # 1) 选择目标日期
        if sel.get("date"):
            self._click_by_text(driver, sel["date"])
        else:
            self._click_date(driver, day)
        time.sleep(0.1)

        # 2) 选择三号场
        if sel.get("place"):
            self._click_by_text(driver, sel["place"])
        else:
            self._click_first_text(
                driver, [place_name, "羽毛球3號場", "羽毛球3号场"], "选择场地"
            )
        time.sleep(0.1)

        # 3) 选择 07:00-08:00 时段
        if sel.get("time_slot"):
            self._click_by_text(driver, sel["time_slot"])
        elif not self._click_time_slot(driver, time_label):
            # 首次失败：重新点日期/场地后重试一次；仍失败则中止，
            # 避免“时段未选中”状态下点提交（order/start 不会触发，页面看似无滑块）。
            log.warning("首次选择时段失败，重选日期/场地后重试")
            self._click_date(driver, day)
            time.sleep(1)
            self._click_first_text(
                driver, [place_name, "羽毛球3號場", "羽毛球3号场"], "选择场地"
            )
            time.sleep(1)
            if not self._click_time_slot(driver, time_label):
                raise RuntimeError(
                    f"选择时段失败：{time_label} 在页面未出现或不可选，已中止（避免无效提交）"
                )
        time.sleep(0.1)

        # 3.5) 勾选“本人已閱讀並同意”条款（提交按钮启用前提）
        if not self._check_agreement(driver):
            log.warning("提交按钮未启用，重试选择时段后再次勾选")
            self._click_time_slot(driver, time_label)
            time.sleep(1)
            self._check_agreement(driver)

        # 4) 提交（演练模式只选不提交）
        if not submit:
            log.info("演练模式：已选中日期/场地/时段/条款，未提交未下单")
            return ""
        if sel.get("submit"):
            self._click_by_text(driver, sel["submit"])
        else:
            try:
                self._click_exact_text(driver, "加入待付款清單", timeout=10)
                log.info("提交预约：已点击「加入待付款清單」")
            except Exception:  # noqa: BLE001
                self._click_first_text(
                    driver,
                    ["確認預約", "確認提交", "確認並繼續", "下一步", "提交", "確認"],
                    "提交预约",
                    must=False,
                )

        captcha = CaptchaHandler(self.cfg)
        # 提交后轮询等待下单信号：order/start 或滑块出现即继续；
        # 6 秒内都没有，说明提交可能没生效，补点其他确认按钮。
        deadline = time.time() + 6
        while time.time() < deadline:
            if self._order_started(recorder) or self._captcha_shown(recorder):
                break
            time.sleep(0.3)
        if not self._order_started(recorder) and not self._captcha_shown(recorder):
            log.warning("提交后未触发下单，尝试点击其他确认按钮……")
            self._log_page_hints(driver)
            for t in ("加入待付款清單", "確認預約", "確認提交", "確認並繼續", "下一步", "提交", "確認"):
                try:
                    self._click_exact_text(driver, t, timeout=3)
                    log.info("已补点「%s」", t)
                    break
                except Exception:  # noqa: BLE001
                    continue
        req = captcha.wait_order_add(recorder, timeout=180)
        recorder.save_body("order_add", req.get("body", ""))
        post = req.get("postData", "")
        if "trerror_" in post:
            # 滑块未拖动/加载失败时腾讯会返回 trerror 错误票据，
            # 前端仍会发 order/add，但服务器不会创建订单（响应体为空）。
            raise RuntimeError(
                "滑块验证未通过：order/add 携带腾讯错误票据（trerror），订单未创建。"
                "通常为滑块资源加载失败或未拖动，请重试并在滑块出现时拖动"
            )
        return self._extract_order_id(req.get("body", ""))

    def _wait_for_booking_widget(self, driver, place_name: str, timeout: float = 40.0) -> None:
        """等待预约小部件加载完成（出现目标场地文本）再开始点击。"""
        log.info("等待预约界面加载……")
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located(
                (By.XPATH, f"//*[contains(normalize-space(.), '{place_name}')]")
            )
        )
        log.info("预约界面已就绪")

    def _order_started(self, recorder) -> bool:
        return any(
            re.search(r"/api/booking/order/start", r.get("url", ""))
            for r in recorder.snapshot()
        )

    def _captcha_shown(self, recorder) -> bool:
        return any(
            "turing.captcha" in r.get("url", "")
            for r in recorder.snapshot()
        )

    def _log_page_hints(self, driver) -> None:
        """输出当前页面上的可点击文本，便于点击失败时定位问题。"""
        try:
            texts = []
            for el in driver.find_elements(
                By.XPATH,
                "//*[self::a or self::button or self::div or self::span or self::li]"
                "[not(self::script)][not(self::style)]",
            )[:120]:
                t = (el.text or "").strip().replace("\n", " ")
                if t and len(t) <= 30:
                    texts.append(t)
            log.info("当前页面可点击文本: %s", " | ".join(dict.fromkeys(texts[:40])))
        except Exception:  # noqa: BLE001
            pass

    # ---------- 交互式选择 ----------
    def choose(self) -> None:
        """交互式选择 场地/日期/时段，保存到 state/selection.json。"""
        s = self.store.load()
        if not s.is_valid():
            with BrowserManager(self.cfg) as bm:
                recorder = bm.recorder()
                s = self.ensure_session(bm.driver, recorder)
        client = BookingClient(
            s.api_key, response_dir=self.cfg.state_dir / "responses"
        )

        log.info("获取场地列表……")
        places_body = client.place_list(
            self.cfg.venue.booking_venue_id, self.cfg.venue.booking_sport_id
        )
        place_list = (places_body.get("data") or {}).get("placeList") or []
        if not place_list:
            raise RuntimeError("未获取到场地列表: " + str(places_body)[:300])
        for i, p in enumerate(place_list, 1):
            print(f"{i}. {p.get('title')}  {p.get('shortTitle', '')}")
        choice = input("请选择场地编号: ").strip()
        if not choice.isdigit() or not (1 <= int(choice) <= len(place_list)):
            raise RuntimeError(f"无效场地编号: {choice!r}")
        place = place_list[int(choice) - 1]
        place_id, place_name = place["id"], place["title"]

        init = client.setting_init()
        day_ranges = (init.get("data") or {}).get("dayRanges") or []
        print("可选日期：")
        for i, d in enumerate(day_ranges, 1):
            print(f"{i}. {d.get('day')}（星期{self._week_cn(d.get('day'))}）")
        print("也可以直接输入 YYYY-MM-DD 自定义日期")
        choice = input("请选择日期编号，或直接输入日期: ").strip()
        if choice.isdigit():
            if not (1 <= int(choice) <= len(day_ranges)):
                raise RuntimeError(f"无效日期编号: {choice!r}")
            day = day_ranges[int(choice) - 1]["day"]
        else:
            day = choice.strip()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                raise RuntimeError(f"无效日期格式（应为 YYYY-MM-DD）: {day!r}")

        try:
            body = client.open_time(place_id, day)
            times = (body.get("data") or {}).get("openTimes") or []
        except Exception as exc:  # noqa: BLE001
            log.warning("查询 %s 时段失败（%s），改用手动输入", day, exc)
            times = []
        if not times:
            print(f"{day} 暂无场次数据（可能尚未放场），可手动输入目标时段")
            tkey = input("请输入时段 timeKey（如 1700）: ").strip()
            if len(tkey) != 4 or not tkey.isdigit():
                raise RuntimeError(f"无效 timeKey（应为 4 位数字，如 1700）: {tkey!r}")
            hh = int(tkey[:2])
            t = {
                "timeKey": tkey,
                "timeFrom": f"{hh:02d}:00",
                "timeTo": f"{hh + 1:02d}:00",
            }
        else:
            print(f"{day} {place_name} 的时段：")
            for i, t in enumerate(times, 1):
                mark = "可预约" if t.get("isCanBook") else "不可约"
                print(
                    f"{i}. {t.get('timeFrom')}-{t.get('timeTo')} [{mark}] "
                    f"{t.get('statusName')} {t.get('price')} MOP"
                )
            choice = input("请选择时段编号: ").strip()
            if not choice.isdigit() or not (1 <= int(choice) <= len(times)):
                raise RuntimeError(f"无效时段编号: {choice!r}")
            t = times[int(choice) - 1]

        sel = Selection(
            place_id=place_id,
            place_name=place_name,
            day=day,
            time_key=str(t.get("timeKey", "")),
            time_label=f"{t.get('timeFrom')}-{t.get('timeTo')}",
            venue_id=self.cfg.venue.booking_venue_id,
            sport_id=self.cfg.venue.booking_sport_id,
        )
        self.selection_store.save(sel)
        log.info("选择已保存：%s %s %s", place_name, day, sel.time_label)

    def _week_cn(self, day: str) -> str:
        try:
            w = datetime.strptime(day, "%Y-%m-%d").weekday()
            return "一二三四五六日"[w]
        except Exception:  # noqa: BLE001
            return "?"

    def _has_pending_payment(self, driver) -> bool:
        try:
            src = driver.page_source
        except Exception:  # noqa: BLE001
            return False
        return "確認付款及保留場地" in src or (
            "獲取驗證碼" in src and "20分鐘內完成付款" in src
        )

    def _click_by_text(self, driver, text: str, timeout: float = 10.0) -> None:
        xpath = (
            f"//*[normalize-space(text())='{text}']"
            f"|//*[contains(normalize-space(.), '{text}')]"
        )
        el = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.XPATH, xpath))
        )
        self._safe_click(driver, el)

    def _click_date(self, driver, day: str) -> None:
        """点击日期格（class=date___3gbyJ），避免误点其他含相同文本的元素。

        事件绑定在 tabItem 容器上：优先点击容器，并在点击后校验 activeTab 选中态，
        避免日期点击未生效导致时段列表停留在停用状态。
        """
        for d in (day[5:], day):
            try:
                cell = WebDriverWait(driver, 6).until(
                    EC.element_to_be_clickable(
                        (
                            By.XPATH,
                            f"//*[contains(concat(' ', normalize-space(@class), ' '), ' date___3gbyJ ')"
                            f" and normalize-space(text())='{d}']",
                        )
                    )
                )
                target = cell
                cur = cell
                for _ in range(3):
                    parent = cur.find_element(By.XPATH, "..")
                    if "tabItem" in (parent.get_attribute("class") or ""):
                        target = parent
                        break
                    cur = parent
                self._safe_click(driver, target)
                WebDriverWait(driver, 5).until(lambda drv: self._date_selected(drv, d))
                log.info("选择目标日期：已点击并确认选中「%s」", d)
                return
            except Exception:  # noqa: BLE001
                continue
        # 兜底：任意元素精确文本
        for d in (day[5:], day):
            try:
                self._click_exact_text(driver, d, timeout=4)
                log.info("选择目标日期：已点击「%s」（兜底）", d)
                return
            except Exception:  # noqa: BLE001
                continue
        raise RuntimeError(f"选择目标日期失败：未找到 {day}")

    def _date_selected(self, driver, d: str) -> bool:
        """目标日期对应的 tabItem 是否处于 activeTab 选中态。"""
        try:
            cells = driver.find_elements(
                By.XPATH,
                f"//*[contains(@class, 'tabItem') and .//*[normalize-space(text())='{d}']]",
            )
        except Exception:  # noqa: BLE001
            return False
        return any("activeTab" in (c.get_attribute("class") or "") for c in cells)

    def _safe_click(self, driver, el) -> None:
        try:
            el.click()
        except Exception:  # noqa: BLE001
            driver.execute_script("arguments[0].click();", el)

    def _click_exact_text(self, driver, text: str, timeout: float = 10.0) -> None:
        """按完全匹配文本点击（避免误中点包含该文本的其他元素）。"""
        el = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable(
                (By.XPATH, f"//*[normalize-space(text())='{text}']")
            )
        )
        self._safe_click(driver, el)

    def _click_time_slot(self, driver, time_label: str, timeout: float = 30.0) -> bool:
        """点击时段；返回是否成功选中。

        scheduleItem 有状态类：stop=停用（日期未生效/时段已过）、select=已选中、
        error=不可约。只点击非 stop 的项，点击后校验是否进入 select/脱离 stop。
        """
        start = time_label.split("-")[0]
        # 页面实际渲染为波浪号分隔（07:00~08:00），优先匹配，避免先等超时
        variants = [time_label.replace("-", "~"), time_label, start]
        deadline = time.time() + timeout
        while time.time() < deadline:
            for v in variants:
                try:
                    item = WebDriverWait(driver, 2).until(
                        EC.presence_of_element_located(
                            (
                                By.XPATH,
                                f"//*[contains(@class, 'scheduleItem') "
                                f"and contains(normalize-space(.), '{v}')]",
                            )
                        )
                    )
                except Exception:  # noqa: BLE001
                    continue
                state = self._item_state(item)
                if state == "stop":
                    continue  # 停用态：日期/场地可能未生效，等下一次循环或由调用方重试
                if state == "select":
                    log.info("选择时段：「%s」已选中", v)
                    return True
                self._safe_click(driver, item)
                try:
                    WebDriverWait(driver, 4).until(
                        lambda d, it=item: "select" in self._item_state(it)
                        or "select" in (it.get_attribute("class") or "")
                    )
                    log.info("选择时段：已点击「%s」", v)
                    return True
                except Exception:  # noqa: BLE001
                    log.warning(
                        "点击「%s」后未进入选中态（state=%s），重试",
                        v,
                        self._item_state(item),
                    )
            time.sleep(0.5)
        log.warning("选择时段失败：%s 时段在 %ss 内未出现或不可选", start, timeout)
        return False

    @staticmethod
    def _item_state(el) -> str:
        """从 scheduleItem 的 class 中提取状态（stop/select/error/…），无则空串。"""
        cls = el.get_attribute("class") or ""
        for token in cls.split():
            if token.startswith(("stop", "select", "error", "active", "book")):
                return token.split("___")[0]
        return ""

    def _check_agreement(self, driver) -> bool:
        """勾选“本人已閱讀並同意”条款（#agreeView），并等待提交按钮启用。"""
        try:
            box = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "#agreeView"))
            )
            self._safe_click(driver, box)
            log.info("已勾选条款（#agreeView）")
        except Exception as exc:  # noqa: BLE001
            log.warning("勾选条款失败: %s", exc)
        try:
            WebDriverWait(driver, 8).until(
                lambda d: "disabled" not in (
                    d.find_element(
                        By.XPATH,
                        "//*[normalize-space(text())='加入待付款清單']",
                    )
                    .find_element(By.XPATH, "..")
                    .get_attribute("class")
                    or ""
                )
            )
            log.info("「加入待付款清單」按钮已启用")
            return True
        except Exception:  # noqa: BLE001
            log.warning("提交按钮仍处于禁用状态")
            return False

    def _click_first_text(self, driver, texts, label: str, must: bool = True) -> None:
        for t in texts:
            try:
                self._click_by_text(driver, t, timeout=5)
                log.info("%s：已点击「%s」", label, t)
                return
            except Exception:  # noqa: BLE001
                continue
        msg = f"{label}失败：未找到可点击元素（尝试过 {texts}）"
        if must:
            raise RuntimeError(msg)
        log.warning(msg)

    def _extract_order_id(self, body: str) -> str:
        try:
            data = json.loads(body)
        except ValueError:
            raise RuntimeError(
                "order/add 响应不是 JSON（订单可能未创建，如滑块票据无效），"
                "原始响应见 state/captures/"
            )
        # 已确认（2026-08-05 HAR）：order/add 返回 {"data":{"number":"IDOB…"}, "code":0}
        node = data.get("data") if isinstance(data.get("data"), dict) else {}
        for key in ("number", "orderId", "orderNo", "id"):
            v = node.get(key) or data.get(key)
            if v:
                return str(v)
        raise RuntimeError("未能在 order/add 响应中找到订单号，见 state/responses/")

    def _payment_flow(self, client: BookingClient, driver, order_id: str) -> None:
        client.payment_info(order_id)

        ctx = SmsContext(order_id=order_id, timeout_seconds=self.cfg.sms.timeout_seconds)
        if isinstance(self.sms_provider, IMessageSmsProvider):
            # 必须在发送验证码之前记录短信时间基线，读取时只接受更新的短信，避免读到旧码
            ctx.newer_than = self.sms_provider.newest_ts()

        captcha_resp = client.send_captcha(order_id)
        if captcha_resp.get("code") != 0:
            raise RuntimeError(
                f"发送验证码失败（{captcha_resp.get('message') or captcha_resp}）。"
                f"订单 {order_id} 已创建但未付款，请先「取消订单」释放场地"
            )
        self.notifier.notify("验证码已发送", "请查收手机短信")
        code = self.sms_provider.get_code(ctx)

        # requestId 必须用 send_captcha 返回的（与验证码配对），不能随机生成
        request_id = ((captcha_resp.get("data") or {}).get("requestId") or "").strip()
        if not request_id:
            raise RuntimeError(
                "send_captcha 响应中未找到 requestId，原始响应见 state/responses/"
            )

        resp = client.payment_start(
            order_id,
            request_id=request_id,
            way=self.cfg.payment.way,
            code=code,
        )
        pay_url = ((resp.get("data") or {}).get("paymentLink") or "").strip() or find_payment_url(resp)
        if not pay_url:
            log.warning(
                "payment/start 响应中未找到 aas.bocmacau.com 支付 URL，原始响应见 state/responses/"
            )
            return
        log.info("打开支付页面: %s", pay_url.split("?")[0])
        if os.environ.get("COURTBOT_OPEN_IN_DEFAULT"):
            import webbrowser

            webbrowser.open(pay_url)
        else:
            driver.get(pay_url)
        self.notifier.notify("请扫码支付", "请用 MPay 扫描二维码完成支付")

        # TODO(集成): 确认 payment/start 返回的 BOC token 字段，再启用自动轮询

    # ---------- 集成辅助 ----------
    def discover(self) -> None:
        """打开浏览器逐步走流程，保存每步页面结构，用于补充选择器。"""
        with BrowserManager(self.cfg) as bm:
            recorder = bm.recorder()
            self._ensure_ready(bm, recorder)
            log.info("已就绪。接下来请按提示手动操作，每一步都会保存页面快照。")
            input("按回车保存当前页面快照（应处于 booking 预约页）……")
            self._dump(bm.driver, "01-booking-page")
            input("请手动选到“选择预约时间”界面后按回车……")
            self._dump(bm.driver, "02-time-page")
            input("请手动点开一个场次并继续到提交/滑块界面后按回车……")
            self._dump(bm.driver, "03-submit-page")
            log.info("页面快照已保存到 state/pages/，请把关键选择器填到 config.yaml 的 selectors")

    def snap(self) -> None:
        """交互式快照：手动操作浏览器，输入 1 保存快照（自动编号），输入 0 退出。"""
        with BrowserManager(self.cfg) as bm:
            recorder = bm.recorder()
            self._ensure_ready(bm, recorder)
            log.info("快照模式已开启：手动操作浏览器，每一步输入 1 保存快照，输入 0 退出。")
            count = 0
            while True:
                try:
                    cmd = input(">>> ").strip()
                except (EOFError, KeyboardInterrupt):
                    log.info("退出快照模式")
                    break
                if cmd == "0":
                    log.info("退出快照模式")
                    break
                if cmd == "1":
                    count += 1
                    self._dump(bm.driver, f"snap-{count:02d}")
                else:
                    print("输入 1 保存快照，输入 0 退出")

    def _dump(self, driver, label: str) -> None:
        log.info("快照 %s | URL: %s", label, driver.current_url)
        pages_dir = self.cfg.state_dir / "pages"

        def collect(d):
            texts = []
            for tag in ("a", "button", "input", "select", "label"):
                for el in d.find_elements(By.TAG_NAME, tag)[:200]:
                    t = (el.text or "").strip().replace("\n", " ")[:60]
                    if t:
                        texts.append(f"<{tag}> {t}")
            return texts

        def save(d, name: str):
            p = pages_dir / name
            p.write_text(d.page_source, encoding="utf-8")
            return p

        main = save(driver, f"{label}.html")
        log.info("已保存: %s（%d 字节）", main, main.stat().st_size)
        texts = collect(driver)

        frames = driver.find_elements(By.TAG_NAME, "iframe")
        for i, fr in enumerate(frames[:10]):
            src = fr.get_attribute("src") or ""
            try:
                driver.switch_to.frame(fr)
            except Exception as exc:  # noqa: BLE001
                log.warning("无法切入 iframe[%d]（%s）: %s", i, src[:80], exc)
                continue
            p = save(driver, f"{label}-iframe{i}.html")
            texts += collect(driver)
            log.info("iframe[%d] 快照已保存: %s（src=%s）", i, p, src[:100])
            driver.switch_to.parent_frame()

        log.info("页面元素示例:\n%s", "\n".join(texts[:80]))
