# -*- coding: utf-8 -*-
"""CAS（秒传文件）解析与还原播放。

``.cas`` 文件本身只有几百字节，内容是一段 Base64 编码的 JSON，记录着原始文件的
名称、大小、MD5 / SHA256 等特征。真正的数据早已在云盘服务器上，只是从你的
文件列表里看不到了。

播放 ``.cas`` 的原理（对照 OpenList ``drivers/139`` 的 ``Put`` + 社区 CAS 分支）：:

    1. 下载 .cas 内容，Base64 + JSON 解出原始文件信息
    2. 用 SHA256 调 /file/create 做「秒传」—— 在临时目录里凭空还原出一个真文件
    3. 取这个临时文件的真实直链，302 给播放器
    4. 停止播放后延迟删除临时文件，云盘里除 .cas 之外不留痕迹

全程不占存储空间、不产生上传流量。

**500GB 支持**：``/file/create`` 带 ``X-Yun-Client-Info`` 客户端类型 13（PC 端）时，
单文件上限是 500GB（Web 端类型 9 只有 50GB）。本模块按官方分片规则生成
``partInfos``（30GB 以下每片 100MB，以上每片 512MB），因为分片大小也是
服务端校验的一部分 —— 分片规则错了，秒传会被拒。
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import logging
import os
import random
import re
import threading
import time

from .errors import NotFoundError, QuotaError, Yun139Error

logger = logging.getLogger("139strm.cas")

MB = 1024 * 1024
GB = 1024 * MB

# 临时还原目录名，放在个人云根目录
TEMP_DIR_NAME = "139STRM_TEMP"

# 还原出来的临时文件默认保留多久后删除（秒）
DEFAULT_TEMP_TTL = 300

# 一个已还原的临时文件允许被复用的**滑动**窗口（秒）。
# 连续播放时播放器每几分钟回来换一次链，每次都把寿命往后推，
# 于是整集自始至终只用一个临时文件 —— 中途换新文件会让播放器
# 手上那条指向旧文件的链接变死链，表现就是「播到一半跳回起点」。
SESSION_TTL = 30 * 60
MAX_SESSION_TTL = SESSION_TTL

# 临时目录 ID 的确认结果缓存多久（秒）。目录被用户删掉的场景由
# 「秒传失败 → 强制重查重试」兜底，所以这个节流是安全的。
DIR_CHECK_INTERVAL = 1800

# 「只还原视频」时的白名单。.iso 是蓝光/DVD 原盘，也是正经视频容器。
VIDEO_EXTS = (
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".ts", ".m2ts",
    ".wmv", ".rmvb", ".rm", ".m4v", ".mpg", ".mpeg", ".3gp",
    ".iso", ".img", ".vob", ".f4v", ".asf", ".mts", ".divx",
)

# .cas 解析结果缓存：取一次 .cas 要两次往返（取下载地址 + GET 内容），
# 内容永不变化，缓存后换链/重播直接省掉两次往返。
CAS_PARSE_CACHE_TTL = 6 * 3600
CAS_PARSE_CACHE_MAX = 500

# 分片规则（与 OpenList / 官方一致）：>30GB 用 512MB，否则 100MB
BIG_FILE_THRESHOLD = 30 * GB
PART_SIZE_SMALL = 100 * MB
PART_SIZE_BIG = 512 * MB
# 单次 /file/create 最多携带的分片数
MAX_PARTS_PER_REQUEST = 100


class CASError(Yun139Error):
    """CAS 解析或还原失败。"""


class CASExpiredError(CASError):
    """云端源数据已不存在，.cas 已失效（重试无益）。"""


class CASInfo:
    """``.cas`` 内记录的原始文件信息。"""

    __slots__ = ("provider", "name", "size", "md5", "slice_md5",
                 "sha1", "sha256", "pre_id", "create_time")

    def __init__(self, provider="", name="", size=0, md5="", slice_md5="",
                 sha1="", sha256="", pre_id="", create_time=""):
        self.provider = provider
        self.name = name
        self.size = int(size or 0)
        self.md5 = md5
        self.slice_md5 = slice_md5
        self.sha1 = sha1
        self.sha256 = sha256
        self.pre_id = pre_id
        self.create_time = create_time

    def to_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d.get(k, "") for k in cls.__slots__})

    def __repr__(self):
        return f"<CASInfo {self.name} ({self.size} bytes, {self.provider})>"


def fmt_size(size):
    """人读的大小。"""
    size = int(size or 0)
    if size >= GB:
        return "%.2f GB" % (size / GB)
    if size >= MB:
        return "%.1f MB" % (size / MB)
    return "%d 字节" % size


def is_cas_name(name):
    """文件名是否以 ``.cas`` 结尾（不区分大小写）。"""
    return (name or "").lower().endswith(".cas")


def video_ext(name):
    return os.path.splitext(name or "")[1].lower()


def is_video_name(name):
    return video_ext(name) in VIDEO_EXTS


# 临时文件名形如 TEMP_<毫秒>_<随机数>_<片名>；云盘重名时会自动加 " (1)"
_TEMP_PREFIX_RE = re.compile(r"^TEMP_\d+_\d+_(?P<base>.*)$")
_RENAME_SUFFIX_RE = re.compile(r"\s*\(\d+\)(?=\.[^.]*$|$)")


def temp_base_name(name):
    """把临时文件名还原成原始片名，用于认出同一部片子的各个副本。

    ``'TEMP_1756..._01357_电影 (1).mkv' -> '电影.mkv'``
    """
    s = (name or "").strip()
    m = _TEMP_PREFIX_RE.match(s)
    if m:
        s = m.group("base")
    return _RENAME_SUFFIX_RE.sub("", s)


def decode(data):
    """Base64 + JSON 解出 CAS 内容。"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    data = (data or b"").strip()
    if not data:
        raise CASError("CAS 内容为空")
    try:
        raw = base64.b64decode(data)
    except (binascii.Error, ValueError) as exc:
        raise CASError(f"CAS 内容不是合法 Base64: {exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CASError(f"CAS 内容不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise CASError("CAS 内容不是 JSON 对象")

    if not payload.get("name") or payload.get("size") is None:
        raise CASError("CAS 内容缺少 name / size 字段")
    if not (payload.get("md5") or payload.get("sha256") or payload.get("sha1")):
        raise CASError("CAS 内容缺少任何一种文件校验值")

    provider = (payload.get("provider") or "").lower()
    if provider == "115" and not (payload.get("sha1") and payload.get("preID")):
        raise CASError("115 的 CAS 内容缺少 sha1 / preID")

    return CASInfo(
        provider=provider,
        name=payload.get("name") or "",
        size=int(payload.get("size") or 0),
        md5=payload.get("md5") or "",
        slice_md5=payload.get("sliceMd5") or payload.get("md5") or "",
        sha1=payload.get("sha1") or "",
        sha256=payload.get("sha256") or "",
        pre_id=payload.get("preID") or "",
        create_time=str(payload.get("create_time") or ""),
    )


def derive_restore_name(cas_name, original_name):
    """由 ``.cas`` 文件名和原始文件名推出还原后的文件名。

    ``电影.mp4.cas`` + ``电影.mp4`` -> ``电影.mp4``；
    ``电影.cas`` + ``电影.mp4`` -> ``电影.mp4``。
    """
    base = os.path.splitext(cas_name)[0]
    # 去掉可能存在的双重后缀（xxx.mp4.cas 以外还有 xxx.mkv.mkv.cas 这种奇怪命名）
    ext = os.path.splitext(original_name)[1]
    if base.lower().endswith(ext.lower()) and ext:
        base = os.path.splitext(base)[0]
    if not base:
        base = os.path.splitext(original_name)[0]
    return base + ext


def resolve_restore_name(cas_name, info):
    """校验并得出还原文件名。"""
    if info is None:
        raise CASError("缺少 CAS 内容")
    if not is_cas_name(cas_name):
        raise CASError(f"文件名 {cas_name!r} 不是 .cas 结尾")
    if not os.path.splitext(cas_name)[0].strip():
        raise CASError(f".cas 文件名 {cas_name!r} 去掉后缀后为空")
    name = derive_restore_name(cas_name, info.name).strip()
    if not name:
        raise CASError(f".cas 文件名 {cas_name!r} 推不出原始文件名")
    if "/" in name or "\\" in name:
        raise CASError(f"还原文件名 {name!r} 含有路径分隔符")
    return name


def part_size_for(size):
    """分片大小：>30GB 用 512MB，否则 100MB（与官方一致）。"""
    return PART_SIZE_BIG if size // GB > 30 else PART_SIZE_SMALL


def build_part_infos(size):
    """生成**全部**分片描述（大文件也会完整生成，只是发送时分批）。"""
    size = int(size or 0)
    if size <= 0:
        raise CASError("文件大小为 0，无法秒传还原")
    ps = part_size_for(size)
    count = 1 if size <= ps else (size + ps - 1) // ps
    return [
        {
            "partNumber": i + 1,
            "partSize": min(size - i * ps, ps),
            "parallelHashCtx": {"partOffset": i * ps},
        }
        for i in range(count)
    ]


class CASRestorer:
    """在移动云盘上把 ``.cas`` 秒传还原成可播放的真文件。

    线程安全：所有共享状态都由 ``self._state_lock`` / 各类单飞锁保护。
    一个实例对应一个账号（由 app 层按账号缓存）。
    """

    def __init__(self, client, temp_ttl=DEFAULT_TEMP_TTL,
                 temp_dir_name=TEMP_DIR_NAME, allow_all_ext=False,
                 cache_file=""):
        self.client = client
        self.temp_ttl = int(temp_ttl or DEFAULT_TEMP_TTL)
        self.temp_dir_name = temp_dir_name
        self.allow_all_ext = bool(allow_all_ext)
        self.max_session_ttl = SESSION_TTL

        self._temp_dir_id = None
        self._dir_checked_at = 0.0
        self._lock = threading.Lock()          # 保护临时目录 ID

        self._state_lock = threading.RLock()   # 保护下面的状态表
        self._pending = {}                     # temp_id -> 到期时间戳
        self._reaper = None
        self._sessions = {}                    # cas_file_id -> session dict
        self._recent_temp_ids = {}             # temp_id -> 时刻（刚被顶替，不能删）
        self._recent_temp_ttl = 3600
        self._flight = {}                      # cas_file_id -> 单飞锁
        self._parse_locks = {}
        self._cas_cache = {}                   # file_id -> (CASInfo, 时刻)
        self._exist_cache = {}                 # temp_id -> (存在?, 时刻)
        self._delete_failures = {}
        self._sweeping = set()
        self._sweep_lock = threading.Lock()

        self._cas_cache_file = cache_file or ""
        self._cas_cache_loaded = False
        self._cas_cache_dirty = False

    # ------------------------------------------------------------------
    # .cas 解析（带缓存 + 单飞）
    # ------------------------------------------------------------------

    def parse(self, file_id, cas_name=""):
        """下载 ``.cas`` 内容并解析出原始文件信息（带落盘缓存）。"""
        self._ensure_cas_cache_loaded()
        now = time.time()
        with self._state_lock:
            hit = self._cas_cache.get(file_id)
        if hit and now - hit[1] < CAS_PARSE_CACHE_TTL:
            return copy.copy(hit[0])

        with self._parse_lock_for(file_id):
            now = time.time()
            with self._state_lock:
                hit = self._cas_cache.get(file_id)
            if hit and now - hit[1] < CAS_PARSE_CACHE_TTL:
                return copy.copy(hit[0])
            url = self.client.get_download_url(file_id)
            try:
                resp = self.client._session.get(url, timeout=self.client.timeout)
            except Exception as exc:      # noqa: BLE001
                raise CASError(f"下载 .cas 内容失败: {exc}") from exc
            try:
                content = resp.content
            finally:
                close = getattr(resp, "close", None)
                if callable(close):
                    close()
            if resp.status_code >= 400:
                raise CASError(f"下载 .cas 内容失败: HTTP {resp.status_code}")
            info = decode(content)
            with self._state_lock:
                self._cas_cache[file_id] = (info, now)
                if len(self._cas_cache) > CAS_PARSE_CACHE_MAX:
                    drop = sorted(self._cas_cache,
                                  key=lambda x: self._cas_cache[x][1])
                    for k in drop[:len(drop) // 4]:
                        self._cas_cache.pop(k, None)
                self._cas_cache_dirty = True
        self._save_cas_cache()
        return info

    def _parse_lock_for(self, file_id):
        with self._state_lock:
            lk = self._parse_locks.get(file_id)
            if lk is None:
                if len(self._parse_locks) > 500:
                    self._parse_locks.clear()
                lk = self._parse_locks[file_id] = threading.Lock()
            return lk

    def _ensure_cas_cache_loaded(self):
        if self._cas_cache_loaded:
            return
        with self._state_lock:
            if self._cas_cache_loaded:
                return
            self._cas_cache_loaded = True
            path = self._cas_cache_file
            if not path or not os.path.exists(path):
                return
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                items = raw.get("items") or {}
                now = time.time()
                for fid, ent in items.items():
                    try:
                        info = CASInfo.from_dict(ent.get("info") or {})
                        ts = float(ent.get("ts") or 0)
                    except (TypeError, ValueError):
                        continue
                    if now - ts < CAS_PARSE_CACHE_TTL:
                        self._cas_cache[fid] = (info, ts)
            except (OSError, ValueError) as exc:
                logger.info("读取 .cas 解析缓存失败（忽略）: %s", exc)

    def _save_cas_cache(self):
        path = self._cas_cache_file
        if not path:
            return
        with self._state_lock:
            if not self._cas_cache_dirty:
                return
            self._cas_cache_dirty = False
            items = {k: {"info": v[0].to_dict(), "ts": v[1]}
                     for k, v in list(self._cas_cache.items())[:CAS_PARSE_CACHE_MAX]}
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"items": items}, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as exc:
            logger.info("写入 .cas 解析缓存失败（忽略）: %s", exc)

    # ------------------------------------------------------------------
    # 临时目录
    # ------------------------------------------------------------------

    def _scan_temp_dir(self):
        """在根目录里查找临时文件夹；查询失败会抛异常（区别于「确实没有」）。"""
        for item in self.client.list_files(self.client.root_folder_id):
            if item.name == self.temp_dir_name and item.is_folder:
                return item.file_id
        return None

    def find_temp_dir(self):
        try:
            return self._scan_temp_dir()
        except Exception as exc:      # noqa: BLE001
            logger.warning("列出根目录失败: %s", exc)
            return None

    def ensure_temp_dir(self):
        """取得（必要时创建）根目录里的临时文件夹。"""
        with self._lock:
            if (self._temp_dir_id and
                    time.time() - self._dir_checked_at < DIR_CHECK_INTERVAL):
                return self._temp_dir_id
            try:
                found = self._scan_temp_dir()
            except Exception as exc:      # noqa: BLE001
                if self._temp_dir_id:
                    logger.warning("列出根目录失败，沿用已记住的临时目录: %s", exc)
                    return self._temp_dir_id
                raise CASError(f"无法列出云盘根目录: {exc}") from exc

            if found:
                if self._temp_dir_id and found != self._temp_dir_id:
                    logger.warning("临时目录 ID 已变化 %s -> %s，已自动更新",
                                   self._temp_dir_id, found)
                self._temp_dir_id = found
                self._dir_checked_at = time.time()
                return found

            if self.client.cloud_type != "personal_new":
                raise CASError("只有新版个人云（personal_new）支持 CAS 秒传还原")

            try:
                resp = self.client.personal_create({
                    "parentFileId": self.client.root_folder_id,
                    "name": self.temp_dir_name,
                    "description": "",
                    "type": "folder",
                    "fileRenameMode": "force_rename",
                })
            except QuotaError as exc:
                raise CASError(f"无法创建临时目录（账号权益不足）: {exc}") from exc
            data = resp.get("data") or {}
            file_id = data.get("fileId") or ""
            if not file_id:
                raise CASError(f"创建临时目录失败: {resp}")

            # force_rename 下若已同名，服务端会改名，需要重新查一次拿正确 ID
            if (data.get("fileName") or self.temp_dir_name) != self.temp_dir_name:
                found = self.find_temp_dir()
                if found:
                    self._temp_dir_id = found
                    self._dir_checked_at = time.time()
                    return found
                raise CASError(
                    f"已存在同名目录 {data.get('fileName')}，且无法定位，请手动删除后重试")

            self._temp_dir_id = file_id
            self._dir_checked_at = time.time()
            logger.info("已创建临时目录 %s (%s)", self.temp_dir_name, file_id)
            return file_id

    def set_temp_dir(self, dir_id):
        with self._lock:
            self._temp_dir_id = dir_id or None

    def get_temp_dir(self):
        with self._lock:
            return self._temp_dir_id

    # ------------------------------------------------------------------
    # 秒传还原
    # ------------------------------------------------------------------

    def _create_by_sha256(self, dir_id, name, size, sha256):
        """在 ``dir_id`` 下按 SHA256 秒传还原文件，返回 (file_id, 云端文件名)。"""
        if len(sha256) != 64:
            raise CASError(f"SHA256 长度不正确: {len(sha256)}")
        parts = build_part_infos(size)
        payload = {
            "contentHash": sha256.lower(),
            "contentHashAlgorithm": "SHA256",
            "contentType": "application/octet-stream",
            "parallelUpload": False,
            "partInfos": parts[:MAX_PARTS_PER_REQUEST],
            "size": size,
            "parentFileId": dir_id,
            "name": name,
            "type": "file",
            "fileRenameMode": "auto_rename",
        }
        t0 = time.perf_counter()
        try:
            resp = self.client.personal_create(payload)
        except QuotaError as exc:
            raise CASError(
                "账号秒传权益不足，无法还原大小约 %s 的文件（%s）。"
                "这是移动云盘账号等级限制，与本站无关；请升级移动云盘会员，"
                "或只播放较小的 CAS 文件。" % (fmt_size(size), exc)) from exc
        took = time.perf_counter() - t0
        data = resp.get("data") or {}
        if not data.get("exist") and not data.get("rapidUpload") \
                and data.get("partInfos") is not None:
            raise CASExpiredError(
                "秒传失败：云端已不存在该文件的源数据，.cas 已失效无法还原")
        file_id = data.get("fileId") or ""
        if not file_id:
            raise CASError(f"秒传还原未返回文件 ID: {resp}")
        if took > 2 or len(parts) > MAX_PARTS_PER_REQUEST:
            logger.info("秒传耗时 %.2fs（%s，分片 %d/%d%s）",
                        took, fmt_size(size), len(parts[:MAX_PARTS_PER_REQUEST]),
                        len(parts), " 已截断" if len(parts) > MAX_PARTS_PER_REQUEST else "")
        return file_id, (data.get("fileName") or name)

    def _create_in_temp_dir(self, temp_name, info):
        """在临时目录里秒传还原；目录 ID 失效时强制重查重试一次。"""
        last_exc = None
        for attempt in (1, 2):
            with self._lock:
                stale = (self._temp_dir_id is not None and
                         time.time() - self._dir_checked_at >= DIR_CHECK_INTERVAL)
            dir_id = self.ensure_temp_dir()
            try:
                fid, real_name = self._create_by_sha256(
                    dir_id, temp_name, info.size, info.sha256)
                return fid, real_name, dir_id
            except CASExpiredError:
                raise
            except Exception as exc:      # noqa: BLE001
                last_exc = exc
                if attempt == 2 or not stale:
                    raise
                with self._lock:
                    self._temp_dir_id = None
                    self._dir_checked_at = 0.0
                logger.warning("秒传失败（%s），强制重查临时目录后重试", exc)
        raise last_exc

    def restore_temp(self, cas_file_id, cas_name):
        """把 ``.cas`` 还原成临时目录里的一个真文件。

        返回 ``(直链, 原始大小, 临时文件ID, 云端实际文件名, 原始片名)``。
        """
        t0 = time.perf_counter()
        info = self.parse(cas_file_id, cas_name)
        preview_name = resolve_restore_name(cas_name, info)
        if not self.allow_all_ext and not is_video_name(preview_name):
            raise CASError(
                f"{preview_name!r} 不是视频文件，已跳过还原（可在设置里开启「还原所有类型」）")
        if not info.sha256:
            raise CASError("CAS 内容缺少 sha256，无法秒传还原")
        if info.size >= 500 * GB:
            # 秒传上限是 PC 客户端的 500GB；再大服务端必然拒绝，提前给出人话。
            raise CASError(
                f"文件大小 {fmt_size(info.size)} 超过移动云盘 500GB 单文件上限")

        self.ensure_temp_dir()
        info.name = preview_name
        temp_name = "TEMP_%d_%05d_%s" % (
            int(time.time() * 1000), random.randint(0, 99999), preview_name)
        temp_id, real_name, _dir = self._create_in_temp_dir(temp_name, info)
        t2 = time.perf_counter()
        logger.info("已秒传还原 %s -> %s (%s)", cas_name, real_name, temp_id)

        try:
            link = self.client.personal_get_link(temp_id)
        except Exception:
            self.delete_quietly(temp_id)
            raise
        if not link:
            self.delete_quietly(temp_id)
            raise CASError("还原成功但未能取得直链")
        t3 = time.perf_counter()
        total = t3 - t0
        if total > 2:
            logger.info("还原耗时 %.2fs（秒传 %.2fs / 取链 %.2fs）: %s",
                        total, t2 - t0, t3 - t2, cas_name)
        return link, info.size, temp_id, real_name, preview_name

    # ------------------------------------------------------------------
    # 会话（同一部片子的临时文件复用）
    # ------------------------------------------------------------------

    def _flight_lock(self, cas_file_id):
        with self._state_lock:
            lock = self._flight.get(cas_file_id)
            if lock is None:
                lock = self._flight[cas_file_id] = threading.Lock()
            return lock

    def refresh_link(self, temp_id):
        """对已存在的临时文件重签一条直链；文件没了返回空串。"""
        try:
            return self.client.personal_get_link(temp_id) or ""
        except NotFoundError:
            return ""
        except Yun139Error as exc:
            logger.info("重签直链失败: %s", exc)
            return ""

    def _session_valid(self, cas_file_id):
        with self._state_lock:
            sess = self._sessions.get(cas_file_id)
        if not sess:
            return None
        if time.time() - sess["seen_at"] > self.max_session_ttl:
            return None
        return sess

    def _active_temp_ids(self):
        now = time.time()
        with self._state_lock:
            ids = {s["temp_id"] for s in self._sessions.values()}
            for tid in list(self._recent_temp_ids):
                if now - self._recent_temp_ids[tid] > self._recent_temp_ttl:
                    self._recent_temp_ids.pop(tid, None)
            return ids | set(self._recent_temp_ids)

    def _remember_temp(self, temp_id):
        with self._state_lock:
            self._recent_temp_ids[temp_id] = time.time()

    def _gc_sessions(self):
        now = time.time()
        with self._state_lock:
            for k in [k for k, v in self._sessions.items()
                      if now - v["seen_at"] > self.max_session_ttl]:
                self._remember_temp(self._sessions[k]["temp_id"])
                self._sessions.pop(k, None)

    def fetch_link(self, cas_file_id, cas_name):
        """取可播放直链，返回值见 ``fetch_link_result``。"""
        return self.fetch_link_result(cas_file_id, cas_name)

    def fetch_link_result(self, cas_file_id, cas_name):
        """取可播放直链，返回 ``(url, size, temp_id, real_name, restored)``。

        ``restored=True`` 表示重新秒传出了一个新文件，``False`` 表示复用旧文件
        只换了链接。同一部片子的并发调用由单飞锁串行，不会各还原一份。
        """
        with self._flight_lock(cas_file_id):
            sess = self._session_valid(cas_file_id)
            if sess and self._temp_file_exists(sess["temp_id"]):
                link = self.refresh_link(sess["temp_id"])
                if link:
                    with self._state_lock:
                        sess["seen_at"] = time.time()
                    self._touch_pending(sess["temp_id"])
                    return link, sess["size"], sess["temp_id"], sess["name"], False
                logger.info("复用临时文件取链失败，重新还原: %s", cas_name)
            elif sess:
                logger.info("临时文件已不存在，重新还原: %s", cas_name)

            link, size, temp_id, real_name, preview_name = self.restore_temp(
                cas_file_id, cas_name)
            with self._state_lock:
                old = self._sessions.get(cas_file_id)
                if old and old["temp_id"] != temp_id:
                    # 旧文件交给延迟删除，并登记为「刚被顶替」——
                    # 播放器可能还攥着指向它的链接，清扫必须绕开。
                    self._remember_temp(old["temp_id"])
                    self.schedule_delete(old["temp_id"])
                self._sessions[cas_file_id] = {
                    "temp_id": temp_id,
                    "name": preview_name,
                    "size": int(size or 0),
                    "created_at": time.time(),
                    "seen_at": time.time(),
                }
            self._gc_sessions()
            self._purge_leftovers_async(preview_name, temp_id)
            return link, size, temp_id, real_name, True

    def forget_session(self, cas_file_id):
        with self._state_lock:
            sess = self._sessions.pop(cas_file_id, None)
        if sess:
            self._remember_temp(sess["temp_id"])
        return sess

    def session_count(self):
        with self._state_lock:
            return len(self._sessions)

    def has_session(self, cas_file_id):
        return self._session_valid(cas_file_id) is not None

    def _temp_file_exists(self, temp_id):
        """确认临时文件还在（结果缓存 20 秒，避免并发请求各自列一遍目录）。"""
        now = time.time()
        with self._state_lock:
            hit = self._exist_cache.get(temp_id)
        if hit and now - hit[1] < 20:
            return hit[0]
        ok = False
        try:
            dir_id = self.ensure_temp_dir()
            ok = any(it.file_id == temp_id
                     for it in self.client.list_files(dir_id))
        except Exception as exc:      # noqa: BLE001
            logger.warning("确认临时文件失败，按存在处理: %s", exc)
            ok = True
        with self._state_lock:
            self._exist_cache[temp_id] = (ok, now)
        return ok

    def _purge_leftovers_async(self, base_name, keep_id):
        """异步清掉同一部片子的其它残留副本（同一时间只跑一个清扫线程）。"""
        with self._sweep_lock:
            if base_name in self._sweeping:
                return
            self._sweeping.add(base_name)

        def _work():
            try:
                self._purge_leftovers(base_name, keep_id)
            except Exception as exc:      # noqa: BLE001
                logger.info("清理旧副本失败（忽略）: %s", exc)
            finally:
                with self._sweep_lock:
                    self._sweeping.discard(base_name)

        threading.Thread(target=_work, daemon=True, name="cas-sweep").start()

    def _purge_leftovers(self, restore_name, keep_id=None):
        """删掉临时目录里同名片子、但不在使用中的副本。"""
        try:
            dir_id = self.ensure_temp_dir()
            items = self.client.list_files(dir_id)
        except Exception as exc:      # noqa: BLE001
            logger.info("列出临时目录失败: %s", exc)
            return 0
        active = self._active_temp_ids()
        removed = 0
        for it in items:
            if it.is_folder or it.file_id == keep_id:
                continue
            if temp_base_name(it.name) != restore_name:
                continue
            if it.file_id in active:
                continue
            self.delete_quietly(it.file_id)
            removed += 1
        return removed

    # ------------------------------------------------------------------
    # 延迟删除
    # ------------------------------------------------------------------

    def delete_quietly(self, file_id):
        """彻底删除一个文件（绕过回收站）；失败只记日志。"""
        if not file_id:
            return False
        try:
            self.client.personal_delete([file_id])
            with self._state_lock:
                self._pending.pop(file_id, None)
                self._exist_cache.pop(file_id, None)
                self._delete_failures.pop(file_id, None)
            return True
        except Exception as exc:      # noqa: BLE001
            with self._state_lock:
                n = self._delete_failures.get(file_id, 0) + 1
                self._delete_failures[file_id] = n
            logger.warning("删除临时文件失败（第 %d 次）%s: %s", n, file_id, exc)
            return False

    def cancel_delete(self, file_id):
        with self._state_lock:
            self._pending.pop(file_id, None)

    def schedule_delete(self, file_id, delay=None):
        """安排一个文件在 ``delay`` 秒后彻底删除（重复调用会顺延）。"""
        if not file_id:
            return
        delay = self.temp_ttl if delay is None else max(0, int(delay))
        with self._state_lock:
            self._pending[file_id] = time.time() + delay
        self._ensure_reaper()

    def _touch_pending(self, temp_id):
        """正在被播放的文件：把删除时间往后推（滑动续期）。"""
        with self._state_lock:
            if temp_id in self._pending:
                self._pending[temp_id] = time.time() + max(self.temp_ttl, SESSION_TTL)

    def _ensure_reaper(self):
        with self._state_lock:
            if self._reaper and self._reaper.is_alive():
                return
            self._reaper = threading.Thread(
                target=self._reap_loop, daemon=True, name="cas-reaper")
            self._reaper.start()

    def _reap_loop(self):
        while True:
            time.sleep(5)
            now = time.time()
            due = []
            with self._state_lock:
                for fid, until in list(self._pending.items()):
                    if until <= now:
                        due.append(fid)
                        self._pending.pop(fid, None)
                if not self._pending and not self._sessions:
                    # 没有待办就退出线程，下次 schedule 时再拉起
                    self._reaper = None
                    return
            for fid in due:
                self.delete_quietly(fid)

    def pending_count(self):
        with self._state_lock:
            return len(self._pending)

    def purge_temp_dir(self, max_age=None):
        """清空临时目录里所有**不在使用中**的文件（手动清理残留）。"""
        limit = self.max_session_ttl if max_age is None else int(max_age)
        now = time.time()
        removed = 0
        try:
            dir_id = self.ensure_temp_dir()
            items = self.client.list_files(dir_id)
        except Exception as exc:      # noqa: BLE001
            raise CASError(f"列出临时目录失败: {exc}") from exc
        active = self._active_temp_ids()
        with self._state_lock:
            sessions = {s["temp_id"]: s for s in self._sessions.values()}
        for it in items:
            if it.is_folder:
                continue
            sess = sessions.get(it.file_id)
            if it.file_id in active and sess and now - sess["seen_at"] < limit:
                continue
            if it.file_id in active and it.file_id not in sessions:
                # 刚被顶替、但可能还在被播放器使用的，给足宽限
                continue
            if self.delete_quietly(it.file_id):
                removed += 1
        with self._state_lock:
            for k in [k for k, v in self._sessions.items()
                      if now - v["seen_at"] >= limit]:
                self._sessions.pop(k, None)
        return removed
