"""Settings from environment variables (and a local .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # python-dotenv is optional; plain environment variables also work.
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}

# Domains that send credit alerts for common Nigerian banks. CHECK these against a real alert email from the
# shop's own bank before going live, and set BANK_ALERT_SENDERS to exactly the domains that bank uses.
DEFAULT_ALERT_DOMAINS = ("moniepoint.com", "opay-nigeria.com", "gtbank.com", "zenithbank.com", "accessbankplc.com",
                         "firstbanknigeria.com", "ubagroup.com", "kudabank.com", "palmpay.com", "fidelitybank.ng")


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int | None) -> int | None:
    if value is None or value.strip() == "":
        return default
    return int(value.strip())


def _list(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None or not value.strip():
        return default
    return tuple(v.strip().lower() for v in value.split(",") if v.strip())


@dataclass
class Settings:
    shop_dir: Path = Path("demo")
    db_path: Path = Path("salesdesk.db")
    timezone: str = "Africa/Lagos"

    # "claude" or "muse" for real shops, "gemini" for free-tier testing, "rules" for keyless demos and tests.
    provider: str = "claude"
    model: str = "claude-sonnet-5-5"
    effort: str = "low"
    use_fallbacks: bool = True
    max_tokens: int = 3000
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"

    # Meta Model API: Muse Spark (chat, receipt photos), Muse Voice Transcribe (voice notes), Muse Image (ad pictures).
    # The key also turns on voice notes, receipt reading and ad advice when PROVIDER is something else.
    meta_api_key: str = ""
    meta_model: str = "muse-spark-1.3"

    # Meta ads (Marketing API, read-only): spend per ad, joined to the chats and paid orders each ad brought.
    # Without a token the brief uses the shop's ads.json sample, labelled as a sample.
    meta_ads_token: str = ""
    meta_ad_account: str = ""  # act_<id> or the plain number
    meta_graph_version: str = "v23.0"

    # Web server: the owner's screens, the demo phone and the webhooks.
    host: str = "127.0.0.1"
    port: int = 8792
    demo_key: str = ""  # if set, every screen and /api/ call needs ?k=<this key>
    demo_tools: bool = True  # the demo phone, test bank alerts and reset; turn off for a real shop
    inbound_token: str = ""  # secret in /inbound/* URLs (email, forwarded TikTok chats, bank alerts)

    # WhatsApp Cloud API.
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_graph_version: str = "v23.0"
    whatsapp_template: str = ""  # approved template for follow-ups after the 24-hour window

    # Instagram messaging (Instagram API with Instagram Login).
    instagram_token: str = ""
    instagram_user_id: str = ""
    instagram_verify_token: str = ""
    instagram_app_secret: str = ""

    # Email replies (SMTP). Inbound email arrives at /inbound/email from a forwarder.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""

    # Telegram: customers can chat to the bot (demos on any phone); the owner's group gets alerts.
    telegram_token: str = ""
    owner_chat_id: int | None = None

    # Bank alerts: read credit-alert emails and match them to orders waiting for money.
    imap_host: str = ""
    imap_user: str = ""
    imap_password: str = ""  # a Gmail app password, never the account password
    imap_folder: str = "INBOX"
    alert_domains: tuple[str, ...] = field(default=DEFAULT_ALERT_DOMAINS)
    auto_confirm: bool = True  # confirm by itself only when the alert is verified and matches exactly one order
    check_alerts_minutes: int = 2

    # Behaviour.
    history_messages: int = 12
    daily_cap: int = 600  # model calls per day, a cost guard
    summary_hour: int = 8
    keep_days: int = 365

    @property
    def fallbacks_enabled(self) -> bool:
        return self.use_fallbacks and self.model in FALLBACK_MODELS

    @property
    def active_model(self) -> str:
        return {"gemini": self.gemini_model, "rules": "rules", "muse": self.meta_model}.get(self.provider, self.model)


def load_settings(env_file: str | os.PathLike | None = ".env") -> Settings:
    if load_dotenv and env_file and Path(env_file).exists():
        load_dotenv(env_file)
    env = os.environ.get
    provider = (env("PROVIDER") or "claude").strip().lower()
    if provider not in {"claude", "muse", "gemini", "rules"}:
        raise ValueError("PROVIDER must be 'claude', 'muse', 'gemini' or 'rules'")
    return Settings(
        shop_dir=Path(env("SHOP_DIR") or "demo"),
        db_path=Path(env("DB_PATH") or "salesdesk.db"),
        timezone=env("TIMEZONE") or "Africa/Lagos",
        provider=provider,
        model=env("MODEL") or "claude-sonnet-5-5",
        effort=env("EFFORT") or "low",
        use_fallbacks=_bool(env("USE_FALLBACKS"), True),
        max_tokens=_int(env("MAX_TOKENS"), 3000) or 3000,
        gemini_api_key=env("GEMINI_API_KEY") or env("GOOGLE_API_KEY") or "",
        gemini_model=env("GEMINI_MODEL") or "gemini-3.5-flash-lite",
        meta_api_key=env("META_API_KEY") or "",
        meta_model=env("META_MODEL") or "muse-spark-1.3",
        meta_ads_token=env("META_ADS_TOKEN") or "",
        meta_ad_account=env("META_AD_ACCOUNT") or "",
        meta_graph_version=env("META_GRAPH_VERSION") or "v23.0",
        host=env("HOST") or "127.0.0.1",
        port=_int(env("PORT"), 8792) or 8792,
        demo_key=env("DEMO_KEY") or "",
        demo_tools=_bool(env("DEMO_TOOLS"), True),
        inbound_token=env("INBOUND_TOKEN") or "",
        whatsapp_token=env("WHATSAPP_TOKEN") or "",
        whatsapp_phone_number_id=env("WHATSAPP_PHONE_NUMBER_ID") or "",
        whatsapp_verify_token=env("WHATSAPP_VERIFY_TOKEN") or "",
        whatsapp_app_secret=env("WHATSAPP_APP_SECRET") or "",
        whatsapp_graph_version=env("WHATSAPP_GRAPH_VERSION") or "v23.0",
        whatsapp_template=env("WHATSAPP_TEMPLATE") or "",
        instagram_token=env("INSTAGRAM_TOKEN") or "",
        instagram_user_id=env("INSTAGRAM_USER_ID") or "",
        instagram_verify_token=env("INSTAGRAM_VERIFY_TOKEN") or "",
        instagram_app_secret=env("INSTAGRAM_APP_SECRET") or "",
        smtp_host=env("SMTP_HOST") or "",
        smtp_port=_int(env("SMTP_PORT"), 587) or 587,
        smtp_user=env("SMTP_USER") or "",
        smtp_password=env("SMTP_PASSWORD") or "",
        smtp_from=env("SMTP_FROM") or env("SMTP_USER") or "",
        telegram_token=env("TELEGRAM_BOT_TOKEN") or "",
        owner_chat_id=_int(env("OWNER_CHAT_ID"), None),
        imap_host=env("IMAP_HOST") or "",
        imap_user=env("IMAP_USER") or "",
        imap_password=env("IMAP_PASSWORD") or "",
        imap_folder=env("IMAP_FOLDER") or "INBOX",
        alert_domains=_list(env("BANK_ALERT_SENDERS"), DEFAULT_ALERT_DOMAINS),
        auto_confirm=_bool(env("AUTO_CONFIRM"), True),
        check_alerts_minutes=_int(env("CHECK_ALERTS_MINUTES"), 2) or 2,
        history_messages=_int(env("HISTORY_MESSAGES"), 12) or 12,
        daily_cap=_int(env("DAILY_CAP"), 600) or 600,
        summary_hour=_int(env("SUMMARY_HOUR"), 8) or 8,
        keep_days=_int(env("KEEP_DAYS"), 365) or 365,
    )
