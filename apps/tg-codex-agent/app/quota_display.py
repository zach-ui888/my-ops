"""Pure display helpers; never access credentials or sessions."""
import math
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def safe_plan(value):
    # Preserve unknown short plan identifiers, never arbitrary payloads.
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,39}', value) else 'unknown'


def plan_name(value):
    value = safe_plan(value)
    return {'plus': 'Plus', 'pro': 'Pro', 'free': 'Free', 'go': 'Go',
            'team': 'Team', 'business': 'Business', 'enterprise': 'Enterprise',
            'edu': 'Edu', 'unknown': '未知'}.get(value.lower(), value)


def window_minutes(value):
    if not isinstance(value, dict):
        return None
    for key, divisor in (('window_minutes', 1), ('limit_window_minutes', 1),
                         ('window_duration_minutes', 1), ('window_seconds', 60),
                         ('window_duration_seconds', 60)):
        raw = value.get(key)
        if isinstance(raw, bool):
            continue
        try:
            minutes = float(raw) / divisor
            if math.isfinite(minutes) and 0 < minutes <= 5256000:
                return minutes
        except (ValueError, TypeError, OverflowError):
            pass
    return None


def window_name(value, slot):
    minutes = window_minutes(value)
    if minutes is None:
        return '短周期额度' if slot == 'primary' else '长周期额度'
    if abs(minutes - 300) <= 1:
        return '5小时额度'
    if abs(minutes - 10080) <= 1:
        return '每周额度'
    if minutes >= 2880 and minutes % 1440 == 0:
        return f'{minutes / 1440:g}天额度'
    if minutes % 60 == 0:
        return f'{minutes / 60:g}小时额度'
    return f'{minutes:g}分钟额度'


def window_icon(value):
    minutes = window_minutes(value)
    return '📅' if minutes is not None and minutes >= 1440 else '⏱️'


def reset_time(value):
    try:
        zone = ZoneInfo(os.getenv('TZ', 'Asia/Shanghai'))
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo('Asia/Shanghai')
    try:
        return datetime.fromtimestamp(value, zone).strftime('%m-%d %H:%M')
    except (ValueError, TypeError, OverflowError, OSError):
        return '未知'


def alert_window(value, slot):
    return (f'{window_icon(value)} {window_name(value, slot)}：剩余 {100-value["used_percent"]:g}%\n'
            f'⏰ 重置时间：{reset_time(value["resets_at"])}')


def alert_text(email, messages, threshold):
    return ('⚠️ Codex 额度告警\n' + f'👤 当前账号：{email}\n' + '\n\n'.join(messages)
            + f'\n\n已达到告警阈值：≤ {threshold:g}%')
