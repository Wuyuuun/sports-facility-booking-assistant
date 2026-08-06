from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def _fields(cls, data: dict | None) -> dict:
    names = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in (data or {}).items() if k in names}


@dataclass
class AccountConfig:
    login_url: str = "https://www.mo.gov.mo/home"
    authorize_url: str = ""
    username: str = ""
    password: str = ""


@dataclass
class VenueConfig:
    name: str = "奧林匹克體育中心 – 羽毛球場 – 羽毛球"
    venue_id: int = 255
    venue_code: str = "IDV0010S0001"
    area_code: str = "1002"
    place_id: str = "66631da8-6ba1-b101-0040-3910435058de"
    booking_venue_id: str = "66631acd-6ba1-b101-0040-39005dcae507"
    booking_sport_id: str = "6560f1c9-9580-2001-0003-ecd27544b7d6"
    sport: int = 1


@dataclass
class BookingConfig:
    release_time: str = "08:00:00"
    offset_days: int = 3
    time_key: str = "0700"
    time_label: str = "07:00-08:00"
    day_of_week: int = -1
    extra_days: int = 2  # 日期列表在可预约范围外额外显示的天数（供「预定」尚未放场的日期）


@dataclass
class PaymentConfig:
    way: str = "Boc"
    manual: bool = True
    poll_interval: float = 2.0


@dataclass
class CaptchaConfig:
    mode: str = "manual"


@dataclass
class SmsConfig:
    mode: str = "manual"
    timeout_seconds: int = 120
    sender_pattern: str = ""


@dataclass
class NotifyConfig:
    sound: bool = True
    app_notification: bool = True


@dataclass
class BrowserConfig:
    headless: bool = False
    user_data_dir: str = "state/chrome-profile"
    fresh_profile: bool = False
    keep_open: bool = False  # 运行结束后保留浏览器窗口，人工关闭（避免支付二维码来不及看）


@dataclass
class Config:
    timezone: str = "Asia/Macau"
    state_dir: Path = field(default_factory=lambda: ROOT / "state")
    account: AccountConfig = field(default_factory=AccountConfig)
    venue: VenueConfig = field(default_factory=VenueConfig)
    booking: BookingConfig = field(default_factory=BookingConfig)
    payment: PaymentConfig = field(default_factory=PaymentConfig)
    captcha: CaptchaConfig = field(default_factory=CaptchaConfig)
    sms: SmsConfig = field(default_factory=SmsConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    selectors: dict = field(default_factory=dict)

    def resolve_paths(self) -> None:
        self.state_dir = Path(self.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "responses").mkdir(parents=True, exist_ok=True)
        (self.state_dir / "pages").mkdir(parents=True, exist_ok=True)


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path) if path else ROOT / "config.yaml"
    raw = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    load_dotenv(ROOT / ".env")

    account = AccountConfig(**_fields(AccountConfig, raw.get("account")))
    account.username = os.getenv("MO_GOV_USERNAME", account.username)
    account.password = os.getenv("MO_GOV_PASSWORD", account.password)

    cfg = Config(
        timezone=raw.get("timezone", "Asia/Macau"),
        state_dir=raw.get("state_dir", ROOT / "state"),
        account=account,
        venue=VenueConfig(**_fields(VenueConfig, raw.get("venue"))),
        booking=BookingConfig(**_fields(BookingConfig, raw.get("booking"))),
        payment=PaymentConfig(**_fields(PaymentConfig, raw.get("payment"))),
        captcha=CaptchaConfig(**_fields(CaptchaConfig, raw.get("captcha"))),
        sms=SmsConfig(**_fields(SmsConfig, raw.get("sms"))),
        notify=NotifyConfig(**_fields(NotifyConfig, raw.get("notify"))),
        browser=BrowserConfig(**_fields(BrowserConfig, raw.get("browser"))),
        selectors=raw.get("selectors") or {},
    )
    cfg.resolve_paths()
    return cfg
