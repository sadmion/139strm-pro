# -*- coding: utf-8 -*-
"""移动云盘（139Yun）协议层：签名、编码与加解密。

本模块与 OpenList ``drivers/139/util.go`` 逐字节对齐，下列细节都踩过坑：

1. ``encode_uri_component``
   Go 用 ``url.QueryEscape``（A-Za-z0-9 与 ``-_.~`` 之外全部转义，空格转 ``+``），
   然后把 ``+`` 还原成 ``%20``，并把 ``%21 %27 %28 %29 %2A`` 还原成 ``! ' ( ) *``。
   注意 ``~`` 是**不转义**的，这正是 Go 与 JS ``encodeURIComponent`` 的差别。

2. ``cal_sign``
   Go 的 ``strings.Split(s, "")`` 按 rune 切分，``sort.Strings`` 按 UTF-8 字节比较 ——
   对 ASCII 输入两者等价，但输入里带中文时（文件名出现在请求体里很常见）
   必须按**字节**排序，否则签名与官方不一致，接口会回「签名错误」。

3. 密钥
   ``KEY_HEX_1`` 是 24 字节（AES-192），``KEY_HEX_2`` 是 16 字节（AES-128），两者都是
   十六进制字符串，使用前需要 hex 解码。

4. Go 风格 JSON
   登录请求体先按 Go ``encoding/json`` 的规则序列化（键名升序、``& < >`` 转成
   ``\\u0026 \\u003c \\u003e``、数字按 ``%v``/最短表示），再做 AES 加密，否则服务端解不开。
"""

from __future__ import annotations

import base64
import decimal
import hashlib
import json
import math
import os
import secrets
import string
import time
from urllib.parse import quote

from Crypto.Cipher import AES

# 第一层加解密密钥（AES-192），hex 解码后为 "scB5IPbIS1QSsulsNrS0lg=="
KEY_HEX_1 = "73634235495062495331515373756c734e7253306c673d3d"
# 第二层解密密钥（AES-128），hex 解码后为 "qPqDw263XgFgL3u8"
KEY_HEX_2 = "7150714477323633586746674c337538"

_ALPHABET = string.ascii_letters + string.digits


# ---------------------------------------------------------------------------
# 编码 / 哈希
# ---------------------------------------------------------------------------

def encode_uri_component(s: str) -> str:
    """等价 Go: ``encodeURIComponent(url.QueryEscape(str) + 手工还原)``。"""
    # safe="-_.~" 对应 Go 不转义的字符；Python 的 quote 默认亦把空格转 %20
    r = quote(s, safe="-_.~")
    return (r.replace("+", "%20")
             .replace("%21", "!")
             .replace("%27", "'")
             .replace("%28", "(")
             .replace("%29", ")")
             .replace("%2A", "*"))


def md5_hex(s: str) -> str:
    """小写 32 位 md5，等价 Go ``utils.GetMD5EncodeStr``。"""
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def sha1_hex(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def sort_runes(s: str) -> str:
    """等价 Go: ``strings.Split(s, "") -> sort.Strings``（按 UTF-8 字节序排序字符）。"""
    return "".join(sorted(s, key=lambda ch: ch.encode("utf-8")))


def cal_sign(body: str, ts: str, rand_str: str) -> str:
    """计算 ``mcloud-sign`` 里的第三段。

    ``body -> encodeURIComponent -> 按字节排序 -> base64``，然后
    ``md5(base64) + md5(ts + ":" + randStr)`` 再取一次 md5 并转大写。
    """
    body = encode_uri_component(body)
    ordered = sort_runes(body)
    encoded = base64.b64encode(ordered.encode("utf-8")).decode("utf-8")
    res = md5_hex(encoded) + md5_hex(ts + ":" + rand_str)
    return md5_hex(res).upper()


def compact_json(data) -> str:
    """紧凑 JSON（无空格），用于计算签名；键顺序不影响签名。"""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def now_ts() -> str:
    """服务器要求的本地时间戳格式 ``2006-01-02 15:04:05``（北京时间）。"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def random_str(n: int = 16) -> str:
    """等价 Go ``random.String(n)``（字母 + 数字）。"""
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


# ---------------------------------------------------------------------------
# AES / PKCS7
# ---------------------------------------------------------------------------

def pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    padding = block_size - len(data) % block_size
    return data + bytes([padding]) * padding


def pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        raise ValueError("pkcs7: data is empty")
    unpadding = data[-1]
    if unpadding == 0 or unpadding > 16 or unpadding > len(data):
        raise ValueError("pkcs7: invalid padding")
    return data[:-unpadding]


def aes_cbc_encrypt(plaintext: bytes, key: bytes, iv: bytes) -> bytes:
    cipher = AES.new(key, AES.MODE_CBC, iv)
    # 块长固定 16，与密钥长度（16/24/32）无关 —— 原实现在 AES-192 下也写错了
    return cipher.encrypt(pkcs7_pad(plaintext, 16))


def aes_cbc_decrypt(ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return pkcs7_unpad(cipher.decrypt(ciphertext))


def aes_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    """等价 Go ``aes_ecb_decrypt``：逐块 ECB 解密后再去 PKCS7 填充。"""
    cipher = AES.new(key, AES.MODE_ECB)
    return pkcs7_unpad(cipher.decrypt(ciphertext))


# ---------------------------------------------------------------------------
# Go 风格 JSON（登录加密体专用）
# ---------------------------------------------------------------------------

def go_json_string(s: str) -> str:
    """对齐 Go ``encoding/json`` 的字符串编码（含 HTML 转义）。"""
    out = ['"']
    for ch in s:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "<":
            out.append("\\u003c")
        elif ch == ">":
            out.append("\\u003e")
        elif ch == "&":
            out.append("\\u0026")
        elif code == 0x2028:
            out.append("\\u2028")
        elif code == 0x2029:
            out.append("\\u2029")
        elif code < 0x20:
            out.append("\\u%04x" % code)
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def go_float_str(f: float) -> str:
    """对齐 Go ``fmt.Sprintf("%v", float64)``（``%g`` + 最短表示）。

    记有效数字位数 nd、十进制指数 e，当 ``e < -4 或 e >= 6`` 时用科学计数法。
    这个细节很致命：登录体里的手机号 ``13800138000`` 会被序列化成
    ``1.3800138e+10``，写错了登录必失败。
    """
    if math.isnan(f):
        return "NaN"
    if math.isinf(f):
        return "+Inf" if f > 0 else "-Inf"
    if f == 0:
        return "0"

    neg = f < 0
    af = abs(f)

    d = decimal.Decimal(repr(af)).normalize()
    sign, digits, exp = d.as_tuple()
    digits_str = "".join(str(x) for x in digits).rstrip("0")
    if not digits_str:
        return "0"
    nd = len(digits_str)
    dp = nd + exp          # value = 0.digits * 10^dp
    e = dp - 1

    prefix = "-" if neg else ""
    if e < -4 or e >= 6:
        mant = digits_str[0] + ("." + digits_str[1:] if nd > 1 else "")
        return "%s%se%s%02d" % (prefix, mant, "+" if e >= 0 else "-", abs(e))
    if dp <= 0:
        return "%s0.%s%s" % (prefix, "0" * (-dp), digits_str)
    if dp >= nd:
        return "%s%s%s" % (prefix, digits_str, "0" * (dp - nd))
    return "%s%s.%s" % (prefix, digits_str[:dp], digits_str[dp:])


def sorted_json_stringify(obj, _from_json: bool = False) -> str:
    """等价 Go ``sortedJsonStringify``：JSON 键名升序、紧凑输出（无空格）。

    与 Go 实现的两处关键对齐：

    * 任意字符串都会**先尝试按 JSON 解析**，成功则递归 —— 所以 ``"13800138000"``
      会被当成数字序列化成 ``1.3800138e+10``。登录体里的手机号正是这样处理的，
      不这么写登录必失败；
    * ``json.Unmarshal`` 到 ``interface{}`` 的数字一律是 float64，因此解析出来的
      数字必须走 ``go_float_str``；而直接写在 Go map 里的 int 用 ``%v`` 输出整数。
      本函数把「非 float 的 int」视为 Go map 里的 int。
    """
    if obj is None:
        return "null"
    if isinstance(obj, str):
        if not _from_json:
            try:
                parsed = json.loads(obj)
            except (ValueError, TypeError):
                return go_json_string(obj)
            return sorted_json_stringify(parsed, _from_json=True)
        return go_json_string(obj)
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, (int, float)):
        if _from_json or isinstance(obj, float):
            return go_float_str(float(obj))
        return str(obj)
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(sorted_json_stringify(i, _from_json) for i in obj) + "]"
    if isinstance(obj, dict):
        pairs = []
        for key in sorted(obj.keys(), key=lambda k: str(k).encode("utf-8")):
            pairs.append(go_json_string(str(key))
                         + ":" + sorted_json_stringify(obj[key], _from_json))
        return "{" + ",".join(pairs) + "}"
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def encrypt_body(body: dict, aes_key_hex: str = KEY_HEX_1) -> str:
    """构造登录用的加密请求体：``base64(iv(16B) + AES-CBC(sorted_json))``。"""
    aes_key = bytes.fromhex(aes_key_hex)
    sorted_json = sorted_json_stringify(body)
    iv = os.urandom(16)
    encrypted = aes_cbc_encrypt(sorted_json.encode("utf-8"), aes_key, iv)
    return base64.b64encode(iv + encrypted).decode("utf-8")


def decrypt_response(resp_body: str, aes_key_hex: str = KEY_HEX_1) -> bytes:
    """解密响应：base64 解开后前 16 字节是 IV，其后是 AES-CBC 密文。"""
    aes_key = bytes.fromhex(aes_key_hex)
    decoded = base64.b64decode(resp_body)
    if len(decoded) < 16:
        raise ValueError("响应过短，不是合法的加密体")
    return aes_cbc_decrypt(decoded[16:], aes_key, decoded[:16])
