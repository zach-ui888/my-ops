import os
import time
from runner_config import runner_config
from quota_display import plan_name, window_name, window_icon, reset_time
from account_client import request


def fetch(refresh=True):
    return request('quota', refresh=refresh, max_age=int(os.getenv('QUOTA_SNAPSHOT_MAX_AGE', '900')))


def is_stale(result):
    return bool(result.get('stale')) or time.time() - result.get('timestamp', 0) > int(os.getenv('QUOTA_SNAPSHOT_MAX_AGE', '900'))


def require_fresh(result):
    if is_stale(result):
        raise RuntimeError('额度数据 stale/不可用')


def get_rate_limits(refresh_if_stale=False):
    result = fetch(refresh_if_stale)
    require_fresh(result)
    limits = result['rate_limits']
    # Existing monitor requires both windows; missing is unknown, never 100% left.
    if any(limits.get(k) is None for k in ('primary', 'secondary')):
        raise RuntimeError('额度窗口未知')
    return limits, result['session'], result['timestamp']


def build_usage_text(threshold=10, refresh_if_stale=True):
    result = fetch(refresh_if_stale)
    identity = result['identity']
    limits = result['rate_limits']
    lines = ['📊 Codex 额度状态', f"👤 当前账号：{identity['email']}",
             f"🔐 登录方式：{identity['method']}",
             f"💳 订阅计划：{plan_name(limits.get('plan_type'))}", f'⚙️ 执行用户：{runner_config().user}']
    age = max(0, int(time.time() - result.get('timestamp', 0)))
    lines.append(f'🕒 数据年龄：{age} 秒')
    if is_stale(result):
        lines.append('⚠️ 额度数据 stale/不可用；刷新后仍无新快照。')
        return '\n'.join(lines)
    for slot in ('primary', 'secondary'):
        value = limits.get(slot)
        lines.extend(['', f'{window_icon(value)} {window_name(value, slot)}'])
        if value is None:
            lines.append('└ 额度状态：未知')
            continue
        used = value['used_percent']
        lines.extend([f'├ 已使用：{used:g}%', f'├ 剩余：{100-used:g}%',
                      f'└ 重置时间：{reset_time(value["resets_at"])}'])
    lines.extend(['', f'⚠️ 告警阈值：剩余 ≤ {threshold:g}%'])
    return '\n'.join(lines)
