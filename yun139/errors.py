# -*- coding: utf-8 -*-
"""统一的异常类型。

把错误分成几类，方便上层决定「重试 / 换令牌 / 直接告诉用户」：

* ``AuthError``      凭据本身不可用（未配置、格式错、已过期），重试无益
* ``QuotaError``     云盘返回「权益不足 / 容量不足 / 超限」，重试无益
* ``NotFoundError``  云端已不存在（.cas 源数据丢失、文件被删）
* ``Yun139Error``    其余接口错误（网络抖动、502、签名错……），可以重试
"""

from __future__ import annotations


class Yun139Error(Exception):
    """移动云盘接口返回错误（默认视为可重试）。"""


class AuthError(Yun139Error):
    """凭据不可用。"""


class QuotaError(Yun139Error):
    """账号权益 / 容量限制，代码层面无法绕过。"""


class NotFoundError(Yun139Error):
    """源数据已不存在。"""
