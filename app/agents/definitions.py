"""Canonical built-in agent definitions.

Definitions are code-owned: tenants can enable an approved agent, but cannot
invent capabilities or grant tools by editing database configuration.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    key: str
    name: str
    description: str
    capabilities: tuple[str, ...]
    maturity: str
    dispatch_ready: bool = False


BUILTIN_AGENT_DEFINITIONS: tuple[AgentDefinition, ...] = (
    AgentDefinition(
        key="monitoring",
        name="Monitoring Agent",
        description="کشف پلتفرم‌ها، رصد تغییرات و آماده‌سازی دانش مستند به منبع.",
        capabilities=("platform.monitor", "platform.discover", "platform.change.detect", "platform.knowledge.extract"),
        maturity="partial",
        dispatch_ready=True,
    ),
    AgentDefinition(
        key="browser_identity",
        name="Browser & Identity Agent",
        description="تعامل کنترل‌شده با پنل‌ها و حساب‌های مجاز از طریق مرورگر.",
        capabilities=("browser.navigate", "browser.capture", "browser.authorized_action"),
        maturity="partial",
    ),
    AgentDefinition(
        key="content",
        name="Content Agent",
        description="تحلیل دانش برند، پیشنهاد محتوا و تولید سناریو و متن.",
        capabilities=("content.strategy.generate", "content.scenario.generate", "content.copy.generate"),
        maturity="partial",
    ),
    AgentDefinition(
        key="video",
        name="Video Production Agent",
        description="تولید صحنه، مدیریت کاراکتر و مونتاژ خروجی ویدیویی.",
        capabilities=("video.avatar.manage", "video.scene.generate", "video.assemble"),
        maturity="partial",
    ),
)
