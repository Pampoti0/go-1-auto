"""Timezone helpers for user-facing timestamps and date windows."""

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

APP_TZ_NAME = os.getenv("APP_TZ", "Asia/Ho_Chi_Minh")

try:
    APP_TZ = ZoneInfo(APP_TZ_NAME)
except Exception:  # noqa: BLE001
    APP_TZ_NAME = "Asia/Ho_Chi_Minh"
    APP_TZ = ZoneInfo(APP_TZ_NAME)


def now() -> datetime:
    return datetime.now(APP_TZ)


def today():
    return now().date()


def time_label() -> str:
    return now().strftime("%H:%M:%S")


def iso_now() -> str:
    return now().isoformat(timespec="seconds")


def _logging_time_tuple(seconds):
    return datetime.fromtimestamp(seconds, APP_TZ).timetuple()


logging.Formatter.converter = staticmethod(_logging_time_tuple)
