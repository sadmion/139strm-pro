# -*- coding: utf-8 -*-
"""移动云盘（139Yun）API 客户端。

与 OpenList ``drivers/139`` 逐接口对齐，重点：

* **PC 客户端身份（单文件 500GB）**
  OpenList 上游注释写明：Web 端 ``X-Yun-Client-Info`` 的客户端类型是 ``9``，
  单文件上限 50GB；PC 端类型是 ``13``，上限 500GB。
  本客户端对**所有**个人云请求都用 PC 身份（``client_type="pc"``，默认），
  这样秒传还原（``/file/create``）可以吃满 500GB 的单文件额度。
  完整 PC 伪装头（设备 ID、版本号、``x-ExpRoute-Code`` 等）保留在
  ``YUN139_PC_FULL_HEADERS=1`` 开关后，默认关闭以保持与上游一致的稳定行为。

* **请求总时限**
  单次超时管不住「一串调用叠起来」。播放路由会给当前线程设一个总时限，
  超时后所有接口快速失败，让播放器立刻重试（重试时状态已热）。

* **令牌续期写回**
  ``refresh_token`` 拿到新令牌后由上层落盘（``on_token_refresh`` 回调），
  否则容器重启后会退回旧令牌，出现「令牌周期性失效」的假象。
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

from . import crypto
from .errors import AuthError, NotFoundError, QuotaError, Yun139Error

logger = logging.getLogger(__name__)

CLOUD_TYPES = ("personal_new", "personal", "family", "group")

# 与官方 Web 端保持一致，避免被风控识别为异常客户端
DEVICE_INFO_WEB = "||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||"
YUN_ORIGIN = "https://yun.139.com"
ROUTE_POLICY_URL = "https://user-njs.yun.139.com/user/route/qryRoutePolicy"

# PC 客户端伪装参数（秒传还原 / 大文件必需）
PC_APP_VERSION = "8.7.2.20260519"
PC_APP_CHANNEL = "10200153"

CN_TZ = timezone(timedelta(hours=8))

# ---------------------------------------------------------------------------
# 线程级状态：总时限 + 接口往返计数
# ---------------------------------------------------------------------------
_deadline = threading.local()
_counters = threading.local()


def count_reset():
    _counters.n = 0
    _counters.ms = 0


def count_snapshot():
    """返回 (与云盘打了几个来回, 合计毫秒)。"""
    return getattr(_counters, "n", 0), getattr(_counters, "ms", 0)


def count_add(ms):
    """手工记一个来回（探测直链走独立连接池，不会自动计数）。"""
    try:
        _counters.n = getattr(_counters, "n", 0) + 1
        _counters.ms = getattr(_counters, "ms", 0) + int(ms)
    except Exception:      # pragma: no cover - 计数失败无关紧要
        pass


def set_request_deadline(seconds):
    """给当前线程设一个云盘接口总时限（秒）；0 表示不限制。"""
    _deadline.until = (time.time() + seconds) if seconds else 0


def clear_request_deadline():
    _deadline.until = 0


def _clamp_timeout(timeout, left):
    """把单次调用的超时压到「本次请求还剩下的总时间」以内。"""
    def clamp(x):
        try:
            x = float(x)
        except (TypeError, ValueError):
            return x
        return max(0.2, min(x, left))

    if isinstance(timeout, (tuple, list)):
        return tuple(clamp(t) for t in timeout)
    return clamp(timeout)


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

class FileItem:
    """统一的文件 / 目录模型。"""

    __slots__ = ("file_id", "name", "size", "is_folder", "modified", "path")

    def __init__(self, file_id, name, size=0, is_folder=False, modified=None, path=""):
        self.file_id = file_id
        self.name = name
        self.size = size
        self.is_folder = is_folder
        self.modified = modified
        self.path = path

    def to_dict(self):
        return {
            "file_id": self.file_id,
            "name": self.name,
            "size": self.size,
            "is_folder": self.is_folder,
            "modified": self.modified.isoformat() if self.modified else None,
            "path": self.path,
        }

    def __repr__(self):
        kind = "DIR " if self.is_folder else "FILE"
        return f"<{kind} {self.name} ({self.file_id})>"


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------

QUOTA_KEYWORDS = ("权益", "不足", "超限", "容量", "过大", "entitlement", "quota")
NOT_FOUND_KEYWORDS = ("不存在", "已删除", "not found", "not exist", "no such")


def is_quota_message(msg: str) -> bool:
    low = (msg or "").lower()
    return any(k.lower() in low for k in QUOTA_KEYWORDS)


def is_not_found_message(msg: str) -> bool:
    low = (msg or "").lower()
    return any(k.lower() in low for k in NOT_FOUND_KEYWORDS)


class Yun139Client:
    """一个账号一条会话；线程安全（共享 requests.Session + 连接池）。"""

    def __init__(self, authorization="", cloud_type="personal_new",
                 mail_cookies="", username="", cloud_id="", timeout=(6, 12),
                 client_type="pc", pc_full_headers=False,
                 on_token_refresh=None):
        """timeout 默认 (连接 6 秒, 读取 12 秒)。

        单次接口给 30 秒会让「9 个来回」串起来拖到一分钟以上，用户看到的就是
        「一直加载中」。压到 (6, 12) 后，最坏也只是快速失败、重试秒开。
        """
        if cloud_type not in CLOUD_TYPES:
            raise Yun139Error(f"不支持的云类型: {cloud_type}，可选 {CLOUD_TYPES}")
        self.authorization = (authorization or "").strip()
        self.cloud_type = cloud_type
        self.mail_cookies = mail_cookies
        self.username = username
        self.cloud_id = cloud_id
        self.timeout = timeout
        # "pc"（默认，单文件 500GB）或 "web"（50GB）
        self.client_type = "pc" if (client_type or "pc").lower() == "pc" else "web"
        self.pc_full_headers = bool(pc_full_headers)
        self.on_token_refresh = on_token_refresh

        self.account = ""
        self.personal_host = ""
        self.group_host = ""
        self.family_host = ""
        self.root_folder_id = "/" if cloud_type == "personal_new" else "root"

        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        self._lock = threading.Lock()
        self._install_deadline_guard()

    # ------------------------------------------------------------------
    # 请求守卫
    # ------------------------------------------------------------------

    def _install_deadline_guard(self):
        raw_request = self._session.request
        client = self

        def _guarded(method, url, **kw):
            until = getattr(_deadline, "until", 0)
            now = time.time()
            if until and now > until:
                raise Yun139Error(
                    "云盘接口响应太慢，已超过本次播放的等待上限 —— "
                    "请重试（重试会快很多）")
            kw.setdefault("timeout", client.timeout)
            if until:
                left = max(0.2, until - now)
                kw["timeout"] = _clamp_timeout(kw["timeout"], left)
            t0 = time.time()
            try:
                return raw_request(method, url, **kw)
            finally:
                try:
                    _counters.n = getattr(_counters, "n", 0) + 1
                    _counters.ms = (getattr(_counters, "ms", 0)
                                    + int((time.time() - t0) * 1000))
                except Exception:      # pragma: no cover
                    pass

        self._session.request = _guarded

    def set_deadline(self, seconds):
        set_request_deadline(seconds)

    def clear_deadline(self):
        set_request_deadline(0)

    def close(self):
        try:
            self._session.close()
        except Exception:      # pragma: no cover
            pass

    # ------------------------------------------------------------------
    # 凭据
    # ------------------------------------------------------------------

    def _split_authorization(self):
        """把 authorization 拆成 (前缀, 账号, 令牌)。"""
        if not self.authorization:
            raise AuthError("尚未配置 Authorization")
        if self.authorization.lower().startswith("basic "):
            raise AuthError("Authorization 不能带 Basic 前缀，请只填写 Basic 后面那串 Base64")
        try:
            decoded = base64.b64decode(self.authorization, validate=False).decode("utf-8")
        except Exception as exc:      # noqa: BLE001 - 统一成 AuthError
            raise AuthError(f"Authorization 不是合法的 Base64: {exc}") from exc
        parts = decoded.split(":")
        if len(parts) < 3:
            raise AuthError("Authorization 格式不正确，应为 pc:账号:令牌 的 Base64")
        return parts[0], parts[1], ":".join(parts[2:])

    def _token_expiry_ms(self):
        """令牌过期时间戳（毫秒）；解析失败返回 None。"""
        try:
            _, _, token = self._split_authorization()
            strs = token.split("|")
            if len(strs) < 4:
                return None
            return int(strs[3])
        except Exception:      # noqa: BLE001
            return None

    def get_expire_time(self):
        """返回令牌过期时间（北京时间），无法解析时返回 None。"""
        ms = self._token_expiry_ms()
        if ms is None:
            return None
        try:
            return datetime.fromtimestamp(ms / 1000, CN_TZ)
        except (ValueError, OverflowError, OSError):
            return None

    def token_remain_ms(self):
        ms = self._token_expiry_ms()
        return None if ms is None else ms - time.time() * 1000

    def account_name(self):
        """Authorization 里解出的账号（手机号）。

        路由查询和 ``commonAccountInfo`` 都要用它；不要依赖 ``init()`` 是否跑过 ——
        否则一旦初始化的顺序有变，就会发出 ``account=""`` 的请求，被服务端回
        「资源不存在」这种毫不相干的报错。
        """
        try:
            return self._split_authorization()[1]
        except AuthError:
            return self.account or self.username or ""

    def refresh_token(self):
        """续期令牌；返回 True 表示凭据发生了变化。

        只有剩余不足 15 天才会真正去刷新（官方同样如此）。
        刷新接口**失败不等于令牌失效** —— 网络抖动时继续用旧令牌，
        不会因为一次跨线路的超时就让用户去重新抓包。
        """
        prefix, account, token = self._split_authorization()
        self.account = account

        strs = token.split("|")
        if len(strs) < 4:
            raise AuthError("Authorization 中的令牌格式不正确")
        try:
            expiration = int(strs[3])
        except ValueError as exc:
            raise AuthError("令牌中的过期时间无法解析") from exc

        remain = expiration - time.time() * 1000
        if remain > 1000 * 60 * 60 * 24 * 15:
            return False
        if remain < 0:
            raise AuthError("Authorization 已过期，请重新获取")

        url = "https://aas.caiyun.feixin.10086.cn:443/tellin/authTokenRefresh.do"
        body = ("<root><token>" + token + "</token><account>" + account +
                "</account><clienttype>656</clienttype></root>")
        try:
            resp = self._session.post(
                url, data=body.encode("utf-8"), timeout=self.timeout,
                headers={"Content-Type": "application/xml"},
            )
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            ret = root.findtext("return")
            if ret != "0":
                desc = root.findtext("desc") or "未知错误"
                raise Yun139Error(f"刷新令牌失败: {desc}")
            new_token = root.findtext("token")
            if not new_token:
                raise Yun139Error("刷新令牌响应中缺少 token")
        except Exception as exc:      # noqa: BLE001
            logger.warning("令牌刷新未成功（%s: %s），令牌本身还剩 %.0f 天，继续使用",
                           type(exc).__name__, str(exc)[:120], remain / 1000 / 86400)
            return False

        self.authorization = base64.b64encode(
            f"{prefix}:{account}:{new_token}".encode("utf-8")).decode("utf-8")
        if callable(self.on_token_refresh):
            try:
                self.on_token_refresh(self.authorization)
            except Exception as exc:      # noqa: BLE001
                logger.warning("令牌续期回调失败: %s", exc)
        return True

    # ------------------------------------------------------------------
    # 请求头
    # ------------------------------------------------------------------

    @property
    def is_family(self):
        return self.cloud_type == "family"

    @property
    def is_group(self):
        return self.cloud_type == "group"

    def _client_info(self):
        """``X-Yun-Client-Info``：客户端类型决定单文件上限（9=50GB，13=500GB）。"""
        client_id = "13" if self.client_type == "pc" else "9"
        return "||" + client_id + "|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||dW5kZWZpbmVk||"

    def _new_headers(self, ts, rand_str, sign):
        """新版接口请求头（等价 Go ``newRequest``）。"""
        return {
            "Accept": "application/json, text/plain, */*",
            "Authorization": "Basic " + self.authorization,
            "Caller": "web",
            "Cms-Device": "default",
            "Mcloud-Channel": "1000101",
            "Mcloud-Client": "10701",
            "Mcloud-Route": "001",
            "Mcloud-Sign": f"{ts},{rand_str},{sign}",
            "Mcloud-Version": "7.14.0",
            "x-DeviceInfo": DEVICE_INFO_WEB,
            "x-huawei-channelSrc": "10000034",
            "x-inner-ntwk": "2",
            "x-m4c-caller": "PC",
            "x-m4c-src": "10002",
            "x-SvcType": "2" if self.is_family else "1",
            "X-Yun-Api-Version": "v1",
            "X-Yun-App-Channel": "10000034",
            "X-Yun-Channel-Source": "10000034",
            "X-Yun-Client-Info": self._client_info(),
            "X-Yun-Module-Type": "100",
            "X-Yun-Svc-Type": "1",
            "Content-Type": "application/json",
        }

    def _pc_device_id(self):
        """稳定的设备 ID：同一账号每次都是同一个，避免风控把设备当成新设备。"""
        acc = (self.account or self.username or "139").encode("utf-8")
        return "OPENLIST" + crypto.md5_hex(acc.decode("utf-8"))[:16].upper() + "-PC"

    def _pc_headers(self, ts, rand_str, sign):
        """完整 PC 客户端伪装头（``pc_full_headers=True`` 时叠加在新版头上）。"""
        device_id = self._pc_device_id()
        device_info = (
            "||11|%s|PC|QkYtMjAyMDAzMTAxNjQ3|%s|| Windows 10 (10.0)"
            "|1920X1040|Q2hpbmVzZSAoU2ltcGxpZmllZCk=|||" % (PC_APP_VERSION, device_id)
        )
        headers = self._new_headers(ts, rand_str, sign)
        headers.update({
            "x-DeviceInfo": device_info,
            "x-huawei-channelSrc": PC_APP_CHANNEL,
            "x-MM-Source": "000",
            "x-yun-api-version": "v1",
            "x-yun-app-channel": PC_APP_CHANNEL,
            "x-yun-client-info": "||13|%s|PC||%s|| Windows 10 (10.0)|1920X1040|||" % (
                PC_APP_VERSION, device_id),
            "x-yun-device-id": device_id,
            "x-yun-device-info": device_info,
            "x-yun-market-source": "000",
            "x-yun-module-type": "100",
            "x-yun-op-type": "1",
            "x-yun-svc-type": "1",
            "x-ExpRoute-Code": "routeCode=%s,type=2",
        })
        return headers

    def _headers_blend(self, ts, rand_str, sign):
        """「PC 客户端类型 + 官方 Web 头」的混合头。

        只替换决定「单文件上限」的那一项 ``X-Yun-Client-Info``（客户端类型 9 -> 13），
        其余头与 OpenList 生产验证过的 Web 头完全一致 —— 这是风险最低的做法：
        接口按 500GB 额度受理，其余校验路径不变。
        """
        headers = self._new_headers(ts, rand_str, sign)
        headers["X-Yun-Client-Info"] = self._client_info()
        return headers

    def _legacy_headers(self, ts, rand_str, sign):
        """旧版接口请求头（等价 Go ``request``，用于 ``yun.139.com`` 上的老接口）。"""
        return {
            "Accept": "application/json, text/plain, */*",
            "CMS-DEVICE": "default",
            "Authorization": "Basic " + self.authorization,
            "mcloud-channel": "1000101",
            "mcloud-client": "10701",
            "mcloud-sign": f"{ts},{rand_str},{sign}",
            "mcloud-version": "7.14.0",
            "Origin": YUN_ORIGIN,
            "Referer": YUN_ORIGIN + "/w/",
            "x-DeviceInfo": DEVICE_INFO_WEB,
            "x-huawei-channelSrc": "10000034",
            "x-inner-ntwk": "2",
            "x-m4c-caller": "PC",
            "x-m4c-src": "10002",
            "x-SvcType": "2" if self.is_family else "1",
            "Inner-Hcy-Router-Https": "1",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # 底层 POST
    # ------------------------------------------------------------------

    def _post(self, url, data, use_new_headers=True, pc_headers=False,
              headers_builder=None):
        """发一个带签名的 POST。

        ``pc_headers``：个人云接口，``X-Yun-Client-Info`` 用 PC 客户端类型 13（500GB）。
        ``headers_builder``：``fn(ts, rand_str, sign) -> dict``，优先于内置头。
        """
        ts = crypto.now_ts()
        rand_str = crypto.random_str(16)
        body = crypto.compact_json(data)
        sign = crypto.cal_sign(body, ts, rand_str)
        if headers_builder is not None:
            headers = headers_builder(ts, rand_str, sign)
        elif pc_headers and self.pc_full_headers:
            headers = self._pc_headers(ts, rand_str, sign)
        elif use_new_headers:
            headers = self._new_headers(ts, rand_str, sign)
        else:
            headers = self._legacy_headers(ts, rand_str, sign)
        try:
            resp = self._session.post(
                url, data=body.encode("utf-8"), headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise Yun139Error(f"网络错误: {exc}") from exc
        if resp.status_code >= 400:
            raise Yun139Error(f"接口返回 HTTP {resp.status_code}: {url}")
        try:
            result = resp.json()
        except ValueError as exc:
            raise Yun139Error(f"接口返回的不是 JSON: {url}") from exc
        if not isinstance(result, dict):
            raise Yun139Error(f"接口返回格式异常: {url}")
        if not result.get("success"):
            msg = result.get("message") or result.get("msg") or f"接口返回失败: {url}"
            if is_quota_message(msg):
                raise QuotaError(msg)
            if is_not_found_message(msg):
                raise NotFoundError(msg)
            raise Yun139Error(msg)
        return result

    def _ensure_hosts(self):
        if not self.personal_host:
            self.init()

    def _personal_post(self, pathname, data):
        self._ensure_hosts()
        if not self.personal_host:
            raise Yun139Error("个人云接入点地址为空，请重新执行「测试连接」")
        return self._post(self.personal_host + pathname, data,
                          use_new_headers=True, pc_headers=True,
                          headers_builder=self._headers_blend)

    def _yun_post(self, pathname, data):
        return self._post(YUN_ORIGIN + pathname, data, use_new_headers=False)

    def _common_account_info(self):
        return {"account": self.account_name(), "accountType": 1}

    def _new_json(self, data):
        merged = {
            "catalogType": 3,
            "cloudID": self.cloud_id,
            "cloudType": 1,
            "commonAccountInfo": self._common_account_info(),
        }
        merged.update(data)
        return merged

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def init(self):
        """校验凭据、必要时续期，并查询各云的接入地址。

        ``_lock`` 只保护**首次初始化**这一段，且用「双重检查」而不是全程持有：
        ``_post`` 内部会走 ``_ensure_hosts``，若把整段都包在锁里，
        一旦有别的路径在锁内发请求就会自锁死；更重要的是别让锁把
        已经初始化好的其它线程挡在门外。
        """
        if self.personal_host and self.account:
            return self
        with self._lock:
            if self.personal_host and self.account:
                return self
            self._init_locked()
        return self

    def _init_locked(self):
        if not self.authorization and self.mail_cookies:
            self.login_with_cookies()
        # 先解出账号，保证路由查询和后续 commonAccountInfo 都有值
        self.account = self.account_name()
        try:
            self.refresh_token()
        except AuthError:
            if not self._relogin_with_cookies():
                raise
            logger.info("令牌已过期，已用邮箱 Cookie 重新登录获取新令牌")

        resp = self._post(ROUTE_POLICY_URL, {
            "userInfo": {
                "userType": 1,
                "accountType": 1,
                "accountName": self.account,
            },
            "modAddrType": 1,
        }, use_new_headers=False)

        personal = group = family = ""
        for item in (resp.get("data") or {}).get("routePolicyList") or []:
            if not isinstance(item, dict):
                continue
            mod = item.get("modName")
            url = item.get("httpsUrl") or ""
            if mod == "personal":
                personal = url
            elif mod == "group":
                group = url
            elif mod == "family":
                family = url

        if not personal:
            raise AuthError(
                "未能从路由策略里取到个人云接入地址 —— 通常意味着 Authorization "
                "无效或已过期（请在浏览器重新抓一次）。接口返回："
                + str(resp.get("data") or resp)[:200])
        if self.cloud_type in ("group", "family") and not group:
            raise AuthError("未能获取群组/家庭云接入地址")
        self.personal_host = personal.rstrip("/")
        self.group_host = (group or "").rstrip("/")
        self.family_host = (family or "").rstrip("/")
        return self

    def _relogin_with_cookies(self):
        if not self.mail_cookies:
            return False
        try:
            self.login_with_cookies()
            return True
        except Exception as exc:      # noqa: BLE001
            logger.warning("用邮箱 Cookie 重新登录失败: %s", str(exc)[:160])
            return False

    def login_with_cookies(self):
        """用 139 邮箱 Cookie 免密换取 Authorization（Cookie 需含 Os_SSo_Sid 与 RMKEY）。"""
        sid, rmkey = extract_fast_login_cookies(self.mail_cookies)
        if not sid or not rmkey:
            raise AuthError(
                "Cookie 中缺少 Os_SSo_Sid 或 RMKEY，请确认复制的是 mail.10086.cn 的完整 Cookie")
        artifact = self._get_single_token(sid, rmkey)
        self.authorization = self._third_party_login(artifact)
        if callable(self.on_token_refresh):
            try:
                self.on_token_refresh(self.authorization)
            except Exception:      # noqa: BLE001
                pass
        return self.authorization

    def _get_single_token(self, sid, rmkey):
        url = ("https://smsrebuild1.mail.10086.cn/setting/s"
               f"?func=umc:getArtifact&sid={sid}&cguid={int(time.time() * 1000)}")
        resp = self._session.get(
            url, headers={"Cookie": f"RMKEY={rmkey}"}, timeout=self.timeout)
        resp.raise_for_status()
        artifact = ((resp.json().get("var") or {}).get("artifact")) or ""
        if not artifact:
            raise AuthError("未能用邮箱 Cookie 换取登录票据，Cookie 可能已失效")
        return artifact

    def _third_party_login(self, dycpwd):
        body = {
            "clientkey_decrypt": "l3TryM&Q+X7@dzwk)qP",
            "clienttype": "886",
            "cpid": "507",
            "dycpwd": dycpwd,
            "extInfo": {"ifOpenAccount": "0"},
            "loginMode": "0",
            "msisdn": self.username,
            "pintype": "13",
            "secinfo": crypto.sha1_hex(f"fetion.com.cn:{dycpwd}").upper(),
            "version": "20250901",
        }
        headers = {
            "hcy-cool-flag": "1",
            "x-huawei-channelSrc": "10246600",
            "x-sdk-channelSrc": "",
            "x-MM-Source": "0",
            "x-UserAgent": "android|23116PN5BC|android15|1.2.6|||1440x3200|10246600",
            "x-DeviceInfo": ("4|127.0.0.1|5|1.2.6|Xiaomi|23116PN5BC||02-00-00-00-00-00"
                             "|android 15|1440x3200|android|||"),
            "Content-Type": "text/plain;charset=UTF-8",
            "Accept-Encoding": "gzip",
            "User-Agent": "okhttp/3.12.2",
        }
        payload = crypto.encrypt_body(body)
        resp = self._session.post(
            "https://user-njs.yun.139.com/user/thirdlogin",
            data=payload.encode("utf-8"), headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        layer1 = crypto.decrypt_response(resp.text)
        hex_inner = json.loads(layer1.decode("utf-8")).get("data") or ""
        if not hex_inner:
            raise AuthError("登录响应缺少 data 字段")
        final_bytes = crypto.aes_ecb_decrypt(bytes.fromhex(hex_inner),
                                             bytes.fromhex(crypto.KEY_HEX_2))
        final = json.loads(final_bytes.decode("utf-8"))
        auth_token = final.get("authToken")
        if not auth_token:
            raise AuthError("登录失败，未能取得 authToken")
        account = final.get("account") or self.username
        self.account = account
        return base64.b64encode(f"pc:{account}:{auth_token}".encode("utf-8")).decode("utf-8")

    # ------------------------------------------------------------------
    # 目录列表
    # ------------------------------------------------------------------

    def list_files(self, folder_id=None):
        """列出目录下的文件与子目录（自动翻页取全量）。"""
        if folder_id in (None, ""):
            folder_id = self.root_folder_id
        if self.cloud_type == "personal_new":
            return self._personal_list(folder_id)
        if self.cloud_type == "personal":
            return self._legacy_personal_list(folder_id)
        if self.cloud_type == "family":
            return self._family_list(folder_id)
        return self._group_list(folder_id)

    def _personal_list(self, parent_file_id, page_size=100):
        """新版个人云目录列表（``/file/list``），带翻页保护。

        翻页保护（三重要）：
          1. 接口偶发把 ``nextPageCursor`` 原样返回（游标不前进）时，
             必须**立刻停**，否则会无限翻页把接口打爆；
          2. 同一页可能被重复返回，按 ``fileId`` 去重，避免目录里出现重复条目；
          3. 最多 2000 页，防止异常数据把内存打满。
        """
        self._ensure_hosts()
        items = []
        by_id = {}
        cursor = ""
        seen_cursors = set()
        pages = 0
        while True:
            data = {
                "imageThumbnailStyleList": ["Small", "Large"],
                "orderBy": "updated_at",
                "orderDirection": "DESC",
                "pageInfo": {"pageCursor": cursor, "pageSize": page_size},
                "parentFileId": parent_file_id,
            }
            resp = self._personal_post("/file/list", data)
            payload = resp.get("data") or {}
            batch = payload.get("items")
            if batch is None:
                batch = payload.get("fileList") or []
            seen_cursors.add(cursor)
            for it in batch:
                if not isinstance(it, dict):
                    continue
                item = FileItem(
                    file_id=it.get("fileId") or it.get("id") or "",
                    name=it.get("name") or "",
                    size=int(it.get("size") or 0),
                    is_folder=(it.get("type") or it.get("category") or "").lower() == "folder",
                    modified=parse_personal_time(it.get("updatedAt")
                                                 or it.get("updateTime")
                                                 or it.get("createdAt") or ""),
                )
                key = item.file_id or (item.name, item.is_folder)
                if key in by_id:
                    continue
                by_id[key] = item
                items.append(item)
            next_cursor = payload.get("nextPageCursor") or ""
            pages += 1
            if (not next_cursor or not batch
                    or next_cursor in seen_cursors      # 游标不前进 → 立刻停
                    or pages >= 2000):
                break
            cursor = next_cursor
        return items

    def _legacy_personal_list(self, catalog_id):
        items = []
        start = 0
        limit = 100
        while True:
            data = {
                "catalogID": catalog_id,
                "sortDirection": 1,
                "startNumber": start + 1,
                "endNumber": start + limit,
                "filterType": 0,
                "catalogSortType": 0,
                "contentSortType": 0,
                "commonAccountInfo": self._common_account_info(),
            }
            resp = self._yun_post("/orchestration/personalCloud/catalog/v1.0/getDisk", data)
            result = ((resp.get("data") or {}).get("getDiskResult")) or {}
            for c in result.get("catalogList") or []:
                items.append(FileItem(
                    file_id=c.get("catalogID"), name=c.get("catalogName") or "",
                    is_folder=True, modified=parse_legacy_time(c.get("updateTime")),
                ))
            for c in result.get("contentList") or []:
                items.append(FileItem(
                    file_id=c.get("contentID"), name=c.get("contentName") or "",
                    size=int(c.get("contentSize") or 0), is_folder=False,
                    modified=parse_legacy_time(c.get("updateTime")),
                ))
            total = int(result.get("totalCount") or len(items))
            start += limit
            if start >= total:
                break
        return items

    def _family_list(self, catalog_id):
        items = []
        page = 1
        while True:
            data = self._new_json({
                "catalogID": "" if catalog_id in (self.root_folder_id, "/") else catalog_id,
                "contentSortType": 0,
                "pageInfo": {"pageNum": page, "pageSize": 100},
                "sortDirection": 1,
            })
            resp = self._yun_post(
                "/orchestration/familyCloud-rebuild/content/v1.2/queryContentList", data)
            payload = resp.get("data") or {}
            for c in payload.get("cloudCatalogList") or []:
                items.append(FileItem(
                    file_id=c.get("catalogID"), name=c.get("catalogName") or "",
                    is_folder=True, modified=parse_legacy_time(c.get("lastUpdateTime")),
                ))
            for c in payload.get("cloudContentList") or []:
                items.append(FileItem(
                    file_id=c.get("contentID"), name=c.get("contentName") or "",
                    size=int(c.get("contentSize") or 0), is_folder=False,
                    modified=parse_legacy_time(c.get("lastUpdateTime")),
                ))
            total = int(payload.get("totalCount") or len(items))
            page += 1
            if (page - 1) * 100 >= total:
                break
        return items

    def _group_list(self, catalog_id):
        items = []
        page = 1
        while True:
            data = self._new_json({
                "groupID": self.cloud_id,
                "catalogID": str(catalog_id).rstrip("/").split("/")[-1],
                "contentSortType": 0,
                "sortDirection": 1,
                "startNumber": page,
                "endNumber": page + 99,
                "path": "/".join([self.root_folder_id.rstrip("/"),
                                  str(catalog_id).lstrip("/")]),
            })
            resp = self._yun_post(
                "/orchestration/group-rebuild/content/v1.0/queryGroupContentList", data)
            result = ((resp.get("data") or {}).get("getGroupContentResult")) or {}
            for c in result.get("catalogList") or []:
                items.append(FileItem(
                    file_id=c.get("catalogID"), name=c.get("catalogName") or "",
                    is_folder=True, modified=parse_legacy_time(c.get("updateTime")),
                    path=c.get("path") or "",
                ))
            for c in result.get("contentList") or []:
                items.append(FileItem(
                    file_id=c.get("contentID"), name=c.get("contentName") or "",
                    size=int(c.get("contentSize") or 0), is_folder=False,
                    modified=parse_legacy_time(c.get("updateTime")),
                    path=c.get("path") or "",
                ))
            total = int(result.get("totalCount") or len(items))
            page += 100
            if page > total:
                break
        return items

    # ------------------------------------------------------------------
    # 直链
    # ------------------------------------------------------------------

    def get_download_url(self, file_id, path=""):
        """获取文件直链（供 302 跳转）。

        新版个人云优先返回 ``cdnUrl``（``cdnSwitch`` 为真时）；否则用 ``url``。
        两者都空时再退一步读 ``downloadURL``。
        """
        if self.cloud_type == "personal_new":
            resp = self._personal_post("/file/getDownloadUrl", {"fileId": file_id})
        elif self.cloud_type == "personal":
            resp = self._yun_post(
                "/orchestration/personalCloud/uploadAndDownload/v1.0/downloadRequest",
                {"appName": "", "contentID": file_id,
                 "commonAccountInfo": self._common_account_info()},
            )
        elif self.cloud_type == "family":
            resp = self._yun_post(
                "/orchestration/familyCloud-rebuild/content/v1.0/getFileDownLoadURL",
                self._new_json({"contentID": file_id, "path": path}),
            )
        else:
            resp = self._yun_post(
                "/orchestration/group-rebuild/groupManage/v1.0/getGroupFileDownLoadURL",
                self._new_json({"contentID": file_id, "groupID": self.cloud_id,
                                "path": path}),
            )
        return self._pick_url(resp.get("data") or {}, resp)

    @staticmethod
    def _pick_url(data, resp):
        cdn_url = data.get("cdnUrl") or ""
        if cdn_url and data.get("cdnSwitch"):
            return cdn_url
        url = data.get("url") or cdn_url
        if not url:
            url = data.get("downloadURL") or ""
        if not url:
            raise Yun139Error(f"未能获取直链: {resp}")
        return url

    def personal_get_link(self, file_id):
        """新版个人云直链（异常时抛出，便于调用方区分「取不到」与「空串」）。"""
        self._ensure_hosts()
        resp = self._personal_post("/file/getDownloadUrl", {"fileId": file_id})
        return self._pick_url(resp.get("data") or {}, resp)

    # ------------------------------------------------------------------
    # 文件操作（秒传还原 / 清理临时文件）
    # ------------------------------------------------------------------

    def personal_create(self, payload):
        """调用 ``/file/create``（建目录，或 SHA256 秒传还原）。"""
        self._ensure_hosts()
        return self._personal_post("/file/create", payload)

    def personal_trash(self, file_ids):
        """移入回收站。"""
        self._ensure_hosts()
        return self._personal_post("/recyclebin/batchTrash", {"fileIds": list(file_ids)})

    def personal_delete(self, file_ids):
        """彻底删除（绕过回收站）。"""
        self._ensure_hosts()
        return self._personal_post("/file/batchDelete", {"fileIds": list(file_ids)})


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def extract_fast_login_cookies(cookie_str):
    sid = rmkey = ""
    for part in (cookie_str or "").split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k == "Os_SSo_Sid":
            sid = v
        elif k == "RMKEY":
            rmkey = v
    return sid, rmkey


def parse_legacy_time(t):
    """旧接口时间格式：``20060102150405``。"""
    if not t:
        return None
    try:
        return datetime.strptime(str(t), "%Y%m%d%H%M%S").replace(tzinfo=CN_TZ)
    except (ValueError, TypeError):
        return None


def parse_personal_time(t):
    """新版接口时间格式：``2026-08-30T21:30:00.000+08:00``。"""
    if not t:
        return None
    try:
        return datetime.fromisoformat(str(t).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return parse_legacy_time(t)
