# -*- coding: utf-8 -*-
"""标准 5 字段 crontab 解析（分 时 日 月 周），用于每个任务独立的定时。

支持：``*``、``*/n``、``a-b``、``a-b/n``、``a,b,c``、纯数字；周字段 0 与 7 都是周日。
"""

from __future__ import annotations

from datetime import datetime, timedelta

FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))

# 现成模板（界面里直接点选）
PRESETS = [
    ("0 3 * * *", "每天 03:00"),
    ("0 */6 * * *", "每 6 小时"),
    ("*/30 * * * *", "每 30 分钟"),
    ("0 3 * * 1", "每周一 03:00"),
]


def expand_field(field: str, lo: int, hi: int):
    """展开单个字段为整数集合；非法返回 None。"""
    if field == "*":
        return set(range(lo, hi + 1))
    out = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            return None
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            if not step_s.isdigit() or int(step_s) <= 0:
                return None
            step = int(step_s)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                return None
            start, end = int(a), int(b)
        else:
            if not part.isdigit():
                return None
            start = end = int(part)
        if start > end or start < 0 or end > hi:
            return None
        out.update(range(max(start, lo), end + 1, step))
    return out or None


def is_valid_cron(expr: str) -> bool:
    parts = (expr or "").split()
    if len(parts) != 5:
        return False
    for f, (lo, hi) in zip(parts, FIELD_RANGES):
        if expand_field(f, lo, hi) is None:
            return False
    return True


def parse_cron(expr: str):
    parts = (expr or "").split()
    if len(parts) != 5:
        return None
    mins = expand_field(parts[0], 0, 59)
    hours = expand_field(parts[1], 0, 23)
    doms = expand_field(parts[2], 1, 31)
    months = expand_field(parts[3], 1, 12)
    dows = expand_field(parts[4], 0, 7)
    if None in (mins, hours, doms, months, dows):
        return None
    dows = {0 if d == 7 else d for d in dows}
    return mins, hours, doms, months, dows, parts[2] != "*", parts[4] != "*"


def _day_match(parsed, dt):
    _m, _h, doms, _mo, dows, dom_restricted, dow_restricted = parsed
    dow = (dt.weekday() + 1) % 7          # Python Monday=0 -> cron Sunday=0
    dom_ok = dt.day in doms
    dow_ok = dow in dows
    if dom_restricted and dow_restricted:
        return dom_ok or dow_ok           # 标准 cron 语义：两者都限制时取「或」
    if dom_restricted:
        return dom_ok
    if dow_restricted:
        return dow_ok
    return True


def cron_matches(expr: str, dt: datetime) -> bool:
    parsed = parse_cron(expr)
    if not parsed:
        return False
    mins, hours, _d, months, _w, _a, _b = parsed
    return (dt.minute in mins and dt.hour in hours
            and dt.month in months and _day_match(parsed, dt))


def next_run_from_cron(expr: str, now: datetime = None, limit_days: int = 400):
    """计算下一次触发时间；表达式非法或 400 天内没有匹配则返回 None。"""
    parsed = parse_cron(expr)
    if not parsed:
        return None
    now = now or datetime.now()
    t = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    end = now + timedelta(days=limit_days)
    while t <= end:
        if cron_matches(expr, t):
            return t
        # 跳过整小时可加快长周期表达式的求解
        if t.minute == 59:
            t = (t + timedelta(minutes=1)).replace(minute=0, second=0, microsecond=0)
        else:
            t = t.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return None
