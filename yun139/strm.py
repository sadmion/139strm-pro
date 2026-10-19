# -*- coding: utf-8 -*-
"""STRM 文件生成器：递归遍历移动云盘目录，为每个媒体文件生成 ``.strm``。

生成规则：

* 云盘 ``电影.mp4``      -> 本地 ``电影.mp4.strm``，内容 ``http://host:port/d/<file_id>``
* 云盘 ``电影.mp4.cas``  -> 本地 ``电影.mp4.strm``，内容 ``.../d/<file_id>?cas=电影.mp4.cas``
  （带 ``?cas=`` 参数，服务端据此走秒传还原）

同步模式：

* ``incremental``：同名 strm 已存在就跳过，只补新增的
* ``force``：全部重新生成、覆盖旧文件
* ``delete_orphans``：本次同步范围内、云端已不存在的 ``.strm`` 会被删除
  （类似 ``rsync --delete``；只删 ``.strm``，绝不动字幕/图片）
"""

from __future__ import annotations

import os
import re
import threading
from datetime import datetime

from .cas import is_cas_name
from .errors import Yun139Error

# 常见的媒体后缀
DEFAULT_MEDIA_EXT = [
    "mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v",
    "ts", "rmvb", "rm", "mpg", "mpeg", "3gp", "iso", "m2ts", "vob",
]

# 常见字幕/附属文件后缀（可选择直接下载到本地）
DEFAULT_COPY_EXT = ["srt", "ass", "ssa", "sub", "vtt", "nfo", "jpg", "png"]

ILLEGAL_CHARS = r'[\\/:*?"<>|\x00-\x1f]'
# 递归深度保护：超过这个深度的目录不再进入（防止异常目录结构把栈打爆）
MAX_DEPTH = 64


def sanitize_name(name: str) -> str:
    """清理非法文件名字符，兼容 Windows / Emby。"""
    s = re.sub(ILLEGAL_CHARS, "_", name or "")
    s = s.strip().rstrip(".").strip()
    return s or "_"


def cas_target_name(name: str) -> str:
    """``.cas`` 文件去掉尾巴后的名字（同样做非法字符清理）。"""
    base = (name or "")[:-4] if is_cas_name(name) else (name or "")
    return sanitize_name(base).lower()


def ext_of(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in (name or "") else ""


class CancelError(Exception):
    """生成任务被手动终止。"""


class StrmGenerator:
    """一次 STRM 同步。实例只用于单次任务，不要复用。"""

    def __init__(self, client, base_url, output_dir,
                 media_ext=None, copy_ext=None, min_size_mb=0,
                 url_encode=True, force=False, delete_orphans=False,
                 progress=None, logger_fn=None):
        self.client = client
        self.base_url = (base_url or "").rstrip("/")
        self.output_dir = output_dir
        self.media_ext = [e.lower().lstrip(".") for e in (media_ext or DEFAULT_MEDIA_EXT)]
        self.copy_ext = [e.lower().lstrip(".") for e in (copy_ext if copy_ext is not None
                                                         else DEFAULT_COPY_EXT)]
        self.min_size = int(float(min_size_mb or 0) * 1024 * 1024)
        self.url_encode = bool(url_encode)
        self.force = bool(force)
        self.delete_orphans = bool(delete_orphans)
        self.progress = progress          # 可选：(扫描数, 生成数) -> None
        self.logger_fn = logger_fn        # 可选：外部日志回调

        self.cancelled = False
        self.created = 0
        self.updated = 0
        self.skipped = 0
        self.copied = 0
        self.deleted = 0
        self.cas_count = 0
        self.scanned = 0
        self.folders = 0
        self.errors = []
        self.log_lines = []

        self._produced = set()
        self._copied_names = set()
        self._cas_hidden_names = set()
        self._touched_dirs = set()
        self._job_root = None
        self._seen_dirs = set()
        self._cancel_event = threading.Event()

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def log(self, msg):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        self.log_lines.append(line)
        if len(self.log_lines) > 1000:
            del self.log_lines[:200]
        if self.logger_fn:
            try:
                self.logger_fn(line)
            except Exception:      # noqa: BLE001
                pass
        return line

    def cancel(self):
        """请求终止；会在下一个检查点抛出 ``CancelError``。

        同时唤醒所有 ``sleep()``，让下载字幕这类阻塞步骤立即退出。
        """
        self.cancelled = True
        self._cancel_event.set()

    def sleep(self, seconds):
        """可被 cancel 打断的 sleep。"""
        if self._cancel_event.wait(seconds):
            raise CancelError("生成已被手动终止")

    def _check_cancel(self):
        if self.cancelled:
            raise CancelError("生成已被手动终止")

    def is_media(self, name):
        if is_cas_name(name):
            return True
        return ext_of(name) in self.media_ext

    def is_copyable(self, name):
        return ext_of(name) in self.copy_ext

    def _strm_url(self, item):
        url = f"{self.base_url}/d/{item.file_id}"
        if is_cas_name(item.name):
            # 注意：这里**不能**对文件名做 percent-encode。
            # 服务端靠 query 里的 ?cas= 名字重新拼还原后的文件名，
            # 编码过的 %E7%94%B5… 会让文件名变成一串百分号转义。
            # Flask/werkzeug 解码 query 时 + 会被当成空格，所以把 + 换成 %2B。
            url += "?cas=" + item.name.replace("+", "%2B")
        return url

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def generate(self, folder_id=None, target_subdir=""):
        """从指定目录开始递归生成。

        ``target_subdir`` 非空时，strm 落在 ``output_dir/<target_subdir>/`` 下
        （任务名就是壳目录）；空时直接落在 ``output_dir`` 下。
        """
        if not self.base_url:
            raise ValueError("基础地址（base_url）不能为空")
        self._check_cancel()
        self._walk(folder_id if folder_id is not None else self.client.root_folder_id,
                   target_subdir, depth=0)
        self.clean_orphans()

    def _walk(self, folder_id, target_subdir, depth):
        self._check_cancel()
        if depth > MAX_DEPTH:
            self.errors.append(f"目录层级超过 {MAX_DEPTH}，已跳过: {target_subdir}")
            return
        key = (folder_id, target_subdir)
        if key in self._seen_dirs:
            return
        self._seen_dirs.add(key)

        try:
            items = self.client.list_files(folder_id)
        except Yun139Error as exc:
            self.errors.append(f"列出目录失败 {target_subdir or '/'}: {exc}")
            self.log(f"列出目录失败 {target_subdir or '/'}: {exc}")
            return

        target_dir = os.path.join(self.output_dir, target_subdir) if target_subdir \
            else self.output_dir
        os.makedirs(target_dir, exist_ok=True)
        if self._job_root is None:
            self._job_root = os.path.abspath(target_dir)
        self._touched_dirs.add(os.path.abspath(target_dir))
        self.folders += 1

        # 先过一遍目录：.cas 已经代表了这部片子，同名的「壳」文件不能再当字幕下载
        for it in items:
            if not it.is_folder and is_cas_name(it.name):
                self._cas_hidden_names.add(cas_target_name(it.name))

        for item in items:
            self._check_cancel()
            self.scanned += 1
            if self.progress and self.scanned % 50 == 0:
                self.progress(self.scanned, self.created + self.updated)
            if item.is_folder:
                sub = sanitize_name(item.name)
                self._walk(item.file_id, os.path.join(target_subdir, sub)
                           if target_subdir else sub, depth + 1)
                continue
            if self.is_media(item.name):
                self._make_strm(item, target_dir)
            elif self.is_copyable(item.name):
                self._copy_file(item, target_dir)
            else:
                self.skipped += 1

    def _make_strm(self, item, target_dir):
        raw_name = item.name
        is_cas = is_cas_name(raw_name)
        name = raw_name[:-4] if is_cas else raw_name      # 去掉 .cas 尾巴
        safe = sanitize_name(name)
        strm_path = os.path.join(target_dir, safe + ".strm")

        # 小于最小体积阈值：不生成。但本地已有同名 strm 要记为「已产出」保留，
        # 否则孤儿清理会把它当成「云端源文件已删除」误删。
        if 0 < self.min_size and item.size < self.min_size:
            self.skipped += 1
            if os.path.exists(strm_path):
                self._produced.add(os.path.abspath(strm_path))
            if is_cas:
                self._cas_hidden_names.add(cas_target_name(raw_name))
            return

        exists = os.path.exists(strm_path)
        if exists and not self.force:
            # 增量：内容一致才算「无需更新」；不一致（比如基础地址改过）就重写
            try:
                with open(strm_path, "r", encoding="utf-8") as fh:
                    old = fh.read().strip()
            except (OSError, ValueError):
                old = None
            new = self._strm_url(item)
            if old == new:
                self.skipped += 1
                self._produced.add(os.path.abspath(strm_path))
                return
            self.log(f"增量：内容已变化，重写 {safe}.strm")


        try:
            with open(strm_path, "w", encoding="utf-8") as fp:
                fp.write(self._strm_url(item))
            self._produced.add(os.path.abspath(strm_path))
            if exists:
                self.updated += 1
            else:
                self.created += 1
                if is_cas:
                    self.cas_count += 1
            if is_cas:
                self.log(("更新 " if exists else "生成 ") + f"{safe}.strm（CAS 秒传）")
        except OSError as exc:
            self.errors.append(f"写入失败 {safe}: {exc}")

    def _copy_file(self, item, target_dir):
        """把字幕等小文件真正下载到本地。"""
        safe = sanitize_name(item.name)
        lower = safe.lower()
        # 同名 .cas 已经代表了这部片子（保留原画质、不占空间）：
        # 云端可能同时躺着一个几百字节的「壳」文件和一张海报/字幕，不能把壳当字幕下载下来。
        for hidden in self._cas_hidden_names:
            if lower == hidden or lower.startswith(hidden + "."):
                self.skipped += 1
                return
        dest = os.path.join(target_dir, safe)
        if os.path.exists(dest) and not self.force:
            self._copied_names.add(safe)
            return
        self._check_cancel()
        try:
            url = self.client.get_download_url(item.file_id)
            resp = self.client._session.get(url, timeout=(10, 60))
            try:
                if resp.status_code >= 400:
                    raise Yun139Error(f"HTTP {resp.status_code}")
                content = resp.content
            finally:
                close = getattr(resp, "close", None)
                if callable(close):
                    close()
            tmp = dest + ".part"
            with open(tmp, "wb") as fp:
                fp.write(content)
            os.replace(tmp, dest)
            self.copied += 1
            self._copied_names.add(safe)
            self.log(f"下载附属文件 {safe}")
        except Exception as exc:      # noqa: BLE001
            self.errors.append(f"下载失败 {safe}: {exc}")

    def clean_orphans(self):
        """删除本任务输出目录里、云端源文件已不存在的 ``.strm``。

        判断依据：本次扫描中云端仍存在的媒体文件，其 strm 都记在 ``_produced``。
        本地还在、但不在此清单里的 ``.strm``，说明云端对应原视频已没了，跟着删。

        递归范围是本任务的输出根目录，因此云端删掉整个子目录时，本地对应子目录
        同样能清干净。不会动其他任务，也不会删字幕/图片。
        """
        if not self.delete_orphans:
            return
        root = self._job_root
        if not root or os.path.abspath(root) == os.path.abspath(self.output_dir):
            dirs = sorted(self._touched_dirs)
        else:
            dirs = [d for d, _sub, _files in os.walk(root)] or [root]
        for d in dirs:
            try:
                entries = os.listdir(d)
            except OSError as exc:
                self.errors.append(f"清理孤儿时无法读取目录 {d}: {exc}")
                continue
            for fn in entries:
                if not fn.lower().endswith(".strm"):
                    continue
                full = os.path.abspath(os.path.join(d, fn))
                if full in self._produced:
                    continue
                try:
                    os.remove(full)
                    self.deleted += 1
                    self.log(f"删除孤儿 {fn}（云端源文件已不存在）")
                except OSError as exc:
                    self.errors.append(f"删除失败 {fn}: {exc}")

    def summary(self):
        return {
            "cancelled": self.cancelled,
            "scanned": self.scanned,
            "folders": self.folders,
            "created": self.created,
            "updated": self.updated,
            "skipped": self.skipped,
            "deleted": self.deleted,
            "cas": self.cas_count,
            "copied": self.copied,
            "errors": self.errors[-50:],
            "error_count": len(self.errors),
            "logs": self.log_lines[-100:],
        }
