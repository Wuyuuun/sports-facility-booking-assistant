from __future__ import annotations

import logging
import time

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

log = logging.getLogger("courtbot.login")

LOGIN_LINK_TEXTS = ("登入", "登录", "登 录", "帳戶登入", "帳戶登錄")


def _url(driver) -> str:
    """current_url 在页面切换瞬间可能为 None，统一兜底为空串。"""
    try:
        return driver.current_url or ""
    except Exception:  # noqa: BLE001
        return ""


def _click_login_link(driver) -> bool:
    for _ in range(6):
        try:
            for tag in ("a", "button"):
                for el in driver.find_elements(By.TAG_NAME, tag):
                    if el.text.strip() in LOGIN_LINK_TEXTS:
                        el.click()
                        return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    return False


def _fill_and_submit(driver, cfg, wait) -> None:
    WebDriverWait(driver, 15).until(
        EC.presence_of_element_located((By.NAME, "username"))
    )
    username = driver.find_element(By.NAME, "username")
    password = driver.find_element(By.NAME, "password")
    username.clear()
    username.send_keys(cfg.account.username)
    password.clear()
    password.send_keys(cfg.account.password)

    submit = driver.find_element(
        By.CSS_SELECTOR, "form button[type=submit], form input[type=submit]"
    )
    submit.click()

    wait.until(
        lambda d: "mo.gov.mo" in _url(d) and "account.gov.mo" not in _url(d)
    )
    log.info("登录成功")


def login(driver, cfg) -> None:
    """完成一户通账号密码登录（登录无验证码）。

    优先走 OAuth 授权入口（config.account.authorize_url）：
    未登录时会被 302 到登录表单，已登录时直接回跳首页。
    若未配置授权入口，则退回“首页找‘登入’按钮”的方式。
    """
    wait = WebDriverWait(driver, 60)

    if cfg.account.authorize_url:
        log.info("打开 OAuth 授权入口: %s", cfg.account.authorize_url.split("?")[0])
        driver.get(cfg.account.authorize_url)
        wait.until(
            lambda d: "account.gov.mo" in _url(d) or "mo.gov.mo" in _url(d)
        )
        if "account.gov.mo" not in _url(driver):
            log.info("已登录（授权入口直接回跳），跳过登录")
            return
        log.info("已进入登录页: %s", _url(driver))
        _fill_and_submit(driver, cfg, wait)
        return

    log.info("打开一户通首页: %s", cfg.account.login_url)
    driver.get(cfg.account.login_url)

    try:
        wait.until(
            lambda d: "mo.gov.mo" in _url(d) and _url(d) != "about:blank"
        )
    except TimeoutException:
        pass

    if "account.gov.mo" not in _url(driver):
        if not _click_login_link(driver):
            log.warning("没有找到“登入”入口，尝试直接访问登录页")
            driver.get("https://account.gov.mo/zh-hant/login")

    try:
        wait.until(lambda d: "account.gov.mo" in _url(d))
    except TimeoutException:
        if "mo.gov.mo" in _url(driver):
            log.info("已在登录状态，跳过登录")
            return
        raise
    log.info("已进入登录页: %s", _url(driver))
    _fill_and_submit(driver, cfg, wait)
