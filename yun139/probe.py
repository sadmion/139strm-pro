# -*- coding: utf-8 -*-
"""链接存活探测：防止把「已经死掉的」直链交给播放器。

为什么需要：播放器 follow 302 之后就**钉死在那条 URL 上**，不会再回来问服务端
要新链。所以交出去的那一条必须是好的。

为什么要有熔断：如果服务器到 CDN 的路不通（很好笑但很常见：甲骨文等海外机访问
国内 CDN 很慢或不通），所有链都会被判成坏链，于是「判坏 → 删掉重还原」变成
每来一个请求还原一份的还原风暴。熔断后一律照常发链，宁可偶尔发一条存疑的，
也绝不重演风暴。
"""

from __future__ import annotations

import threading
import time

import requests

# 探测熔断相关（线程安全由 _lock 保护）
BREAKER_THRESHOLD = 3            # 连续 N 次判定就熔断
BREAKER_SECONDS = 300            # 熔断持续秒数
PROBE_TIMEOUT = 2.0              # 单次探测超时（秒）


class LinkProbe:
    """探测 + 熔断状态机。

    ``probe(url)`` 返回 True/False 表示「能用/不能用」，返回 None 表示「说不清」
    （网络问题、CDN 还在回源等），调用方据此决定是否判死。
    """

    def __init__(self, session=None, timeout=PROBE_TIMEOUT):
        self.timeout = timeout
        self._lock = threading.Lock()
        self._session = session or requests.Session()
        self._session.headers.update({"Range": "bytes=0-0",
                                      "User-Agent": "139StrmPro/probe"})
        self.fail_streak = 0
        self.dead_streak = 0
        self.open_until = 0.0
        self.last_latency_ms = 0

    # ------------------------------------------------------------------

    @property
    def breaker_open(self):
        with self._lock:
            return time.time() < self.open_until

    def usable(self):
        """探测设施本身是否可用（熔断期间返回 False）。"""
        return not self.breaker_open

    def note_ok(self):
        with self._lock:
            self.fail_streak = 0
            self.dead_streak = 0

    def note_unreachable(self):
        """探测本身没探成（连不上/超时）。"""
        with self._lock:
            self.fail_streak += 1
            if self.fail_streak >= BREAKER_THRESHOLD * 2:
                self.open_until = time.time() + BREAKER_SECONDS
                self.fail_streak = 0

    def note_fresh_dead(self):
        """刚还原出来的新链被判死 —— 连着几条必然是探测方的问题。"""
        with self._lock:
            self.dead_streak += 1
            if self.dead_streak >= BREAKER_THRESHOLD:
                self.open_until = time.time() + BREAKER_SECONDS
                self.dead_streak = 0
                self.fail_streak = 0
                return True
            return False

    def reset_breaker(self):
        with self._lock:
            self.open_until = 0.0
            self.fail_streak = 0
            self.dead_streak = 0

    # ------------------------------------------------------------------

    def probe(self, url, timeout=None):
        """拉 1 个字节验活。

        返回值：
          True  —— 明确可用（200/206 且有正常长度语义）
          False —— 明确不可用（401/403/404/410 等）
          None  —— 说不清（超时、连接失败、5xx）
        """
        if not url:
            return False
        t0 = time.time()
        try:
            resp = self._session.get(url, timeout=timeout or self.timeout, stream=True)
            self.last_latency_ms = int((time.time() - t0) * 1000)
            try:
                code = resp.status_code
                if code in (200, 206):
                    cl = resp.headers.get("Content-Length")
                    cr = resp.headers.get("Content-Range")
                    # 刚还原的文件在 CDN 上可能是冷的：200 却没有长度信息，
                    # 一次说不清，两次才敢判死（由调用方的 suspect 机制处理）
                    if cl is None and cr is None:
                        return None
                    return True
                if code in (401, 403, 404, 410):
                    return False
                return None
            finally:
                resp.close()
        except requests.RequestException:
            self.last_latency_ms = int((time.time() - t0) * 1000)
            return None

    def status(self):
        with self._lock:
            return {
                "breaker_open": time.time() < self.open_until,
                "breaker_left": max(0, int(self.open_until - time.time())),
                "fail_streak": self.fail_streak,
                "dead_streak": self.dead_streak,
                "last_latency_ms": self.last_latency_ms,
            }
