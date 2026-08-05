from __future__ import annotations

import logging
import json
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
from courtbot.sms import SmsContext, make_sms_provider

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
        s.api_key = recorder.find_api_key()
        if not s.api_key:
            time.sleep(5)
            s.api_key = recorder.find_api_key()
        if not s.api_key:
            raise RuntimeError(
                "未捕获到 booking.sport.gov.mo 的 api-key 请求头。"
                "请先运行 `python main.py discover` 确认 security 页面加载流程"
            )
        s.fetched_at = time.time()
        self.store.save(s)
        log.info("会话建立完成")
        return s

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
            fresh_key = recorder.find_api_key()
            if not fresh_key:
                time.sleep(3)
                fresh_key = recorder.find_api_key()
            if fresh_key and fresh_key != s.api_key:
                log.info("api-key 已刷新")
                s.api_key = fresh_key
            s.fetched_at = time.time()
            self.store.save(s)
            time.sleep(2)
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

    def _wait_until_release(self, day: str | None = None) -> None:
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
                return
            log.info("放场时间: %s", release.isoformat())
            while True:
                now = datetime.now(tz)
                if now >= release:
                    log.info("放场时间到")
                    return
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
                return
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
            log.info(
                "%s-%s  status=%-8s canBook=%s  price=%s",
                t.get("timeFrom"),
                t.get("timeTo"),
                t.get("statusName") or t.get("status"),
                t.get("isCanBook"),
                t.get("price"),
            )

    # ---------- 抢场 ----------
    def book(self, dry_run: bool = False) -> None:
        sel = self.selection_store.load()
        place_id = sel.place_id or self.cfg.venue.place_id
        place_name = sel.place_name or self.cfg.venue.name
        time_key = sel.time_key or self.cfg.booking.time_key
        time_label = sel.time_label or self.cfg.booking.time_label
        day = sel.day or self.target_day()
        if sel.day:
            log.info("使用已保存选择: %s %s %s", place_name, day, time_label)

        with BrowserManager(self.cfg) as bm:
            recorder = bm.recorder()
            s = self._ensure_ready(bm, recorder)
            client = BookingClient(
                s.api_key, response_dir=self.cfg.state_dir / "responses"
            )

            self._wait_until_release(day)

            if dry_run:
                log.info("dry-run：仅查询余量，不下单")
                client.open_time(place_id, day)
                return

            order_id = self._book_via_ui(bm.driver, recorder, day, place_name, time_label)
            log.info("订单号: %s", order_id)
            self.notifier.notify("抢场成功", f"订单 {order_id} 已创建，进入支付环节")
            self._payment_flow(client, bm.driver, order_id)

    def _book_via_ui(
        self, driver, recorder: FlowRecorder, day: str, place_name: str, time_label: str
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
        self._log_page_hints(driver)
        sel = self.cfg.selectors
        # 1) 选择目标日期
        if sel.get("date"):
            self._click_by_text(driver, sel["date"])
        else:
            self._click_date(driver, day)
        time.sleep(1)

        # 2) 选择三号场
        if sel.get("place"):
            self._click_by_text(driver, sel["place"])
        else:
            self._click_first_text(
                driver, [place_name, "羽毛球3號場", "羽毛球3号场"], "选择场地"
            )
        time.sleep(1)

        # 3) 选择 07:00-08:00 时段
        if sel.get("time_slot"):
            self._click_by_text(driver, sel["time_slot"])
        else:
            self._click_time_slot(driver, time_label)
        time.sleep(1)

        # 3.5) 勾选“本人已閱讀並同意”条款（提交按钮启用前提）
        if not self._check_agreement(driver):
            log.warning("提交按钮未启用，重试选择时段后再次勾选")
            self._click_time_slot(driver, time_label)
            time.sleep(1)
            self._check_agreement(driver)

        # 4) 提交（找不到按钮也没关系，用户可手动点，程序继续等待 order/add）
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
        # 提交后等 12 秒：若既没有下单请求也没有滑块，说明提交没生效，补点其他确认按钮
        time.sleep(12)
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
        return self._extract_order_id(req.get("body", ""))

    def _wait_for_booking_widget(self, driver, place_name: str) -> None:
        """等待预约小部件加载完成（出现目标场地文本）再开始点击。"""
        log.info("等待预约界面加载……")
        WebDriverWait(driver, 40).until(
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
            )[:300]:
                t = (el.text or "").strip().replace("\n", " ")
                if t and len(t) <= 30:
                    texts.append(t)
            log.info("当前页面可点击文本: %s", " | ".join(dict.fromkeys(texts[:50])))
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
        """点击日期格（class=date___3gbyJ），避免误点其他含相同文本的元素。"""
        for d in (day[5:], day):
            try:
                el = WebDriverWait(driver, 6).until(
                    EC.element_to_be_clickable(
                        (
                            By.XPATH,
                            f"//*[contains(concat(' ', normalize-space(@class), ' '), ' date___3gbyJ ')"
                            f" and normalize-space(text())='{d}']",
                        )
                    )
                )
                self._safe_click(driver, el)
                log.info("选择目标日期：已点击「%s」", d)
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

    def _click_time_slot(self, driver, time_label: str) -> None:
        """点击时段：等待时段列表渲染，优先点所在行（scheduleItem），最多等 30 秒。"""
        start = time_label.split("-")[0]
        variants = [time_label, time_label.replace("-", "~"), start]
        deadline = time.time() + 30
        while time.time() < deadline:
            for v in variants:
                try:
                    el = WebDriverWait(driver, 4).until(
                        EC.presence_of_element_located(
                            (
                                By.XPATH,
                                f"//*[contains(normalize-space(.), '{v}') and not(self::script)]",
                            )
                        )
                    )
                    cur = el
                    for _ in range(5):
                        if "scheduleItem" in (cur.get_attribute("class") or ""):
                            self._safe_click(driver, cur)
                            log.info("选择时段：已点击「%s」", v)
                            return
                        cur = cur.find_element(By.XPATH, "..")
                    self._safe_click(driver, el)
                    log.info("选择时段：已点击「%s」", v)
                    return
                except Exception:  # noqa: BLE001
                    continue
            time.sleep(1)
        log.warning("选择时段失败：%s 时段在 30 秒内未出现", start)

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
            raise RuntimeError("order/add 响应不是 JSON，见 state/responses/ 中的原始响应")
        # TODO(集成): 根据实际响应结构调整字段名
        for key in ("orderId", "orderNo", "id"):
            v = data.get("data", {}).get(key) or data.get(key)
            if v:
                return str(v)
        raise RuntimeError("未能在 order/add 响应中找到订单号，见 state/responses/")

    def _payment_flow(self, client: BookingClient, driver, order_id: str) -> None:
        client.payment_info(order_id)
        client.send_captcha(order_id)
        self.notifier.notify("验证码已发送", "请查收手机短信")

        ctx = SmsContext(order_id=order_id, timeout_seconds=self.cfg.sms.timeout_seconds)
        code = self.sms_provider.get_code(ctx)

        import uuid

        resp = client.payment_start(
            order_id,
            request_id=uuid.uuid4().hex,
            way=self.cfg.payment.way,
            code=code,
        )
        pay_url = find_payment_url(resp)
        if not pay_url:
            log.warning(
                "payment/start 响应中未找到 aas.bocmacau.com 支付 URL，原始响应见 state/responses/"
            )
            return
        log.info("打开支付页面: %s", pay_url.split("?")[0])
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
