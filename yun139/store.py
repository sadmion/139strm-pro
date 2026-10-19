# -*- coding: utf-8 -*-
"""配置与任务存储。

设计目标：**永远不要丢配置**。规则：

* ``<CONFIG_PATH>.bak``：每次保存前把旧文件留一份备份；
* ``<CONFIG_PATH>.tmp`` + ``os.replace``：原子写入，断电/崩溃不会写坏；
* 读坏时依次尝试 ``主文件 -> .bak``，并把坏文件另存为 ``.corrupt-<时间>`` 供排查；
* 任何字段超类型/缺失都回落到默认值，不会因为一个坏字段整份配置失效。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime

from .cas import DEFAULT_TEMP_TTL
from .strm import DEFAULT_COPY_EXT, DEFAULT_MEDIA_EXT

DEFAULT_CONFIG = {
    # ---- 移动云盘 ----
    "authorization": "",
    "cloud_type": "personal_new",
    "mail_cookies": "",
    "username": "",
    "cloud_id": "",
    # 单文件上限：pc=500GB（默认），web=50GB
    "client_type": "pc",
    "pc_full_headers": False,
    # ---- 输出 ----
    "output_dir": "/strm",
    "base_url": "",                  # 留空则自动取请求里的 Host
    "media_ext": DEFAULT_MEDIA_EXT,
    "copy_ext": DEFAULT_COPY_EXT,
    "min_size_mb": 0,
    "url_encode": True,
    # ---- CAS 秒传还原 ----
    "cas_enabled": True,
    "cas_temp_ttl": DEFAULT_TEMP_TTL,
    "cas_allow_all_ext": False,
    "cas_temp_dir_id": "",
    # ---- 播放方式 ----
    "play_proxy": False,
    # ---- 界面 ----
    "site_title": "139Strm Pro",
}

_CONFIG_LOCK = threading.RLock()


def _config_path_from_env():
    return os.environ.get("CONFIG_PATH") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")


def _atomic_write_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


class Store:
    """配置 + 任务列表的读写门面。"""

    def __init__(self, config_path=None):
        self.config_path = config_path or _config_path_from_env()
        self.data_dir = os.path.dirname(os.path.abspath(self.config_path)) or "."
        self.tasks_path = os.path.join(self.data_dir, "tasks.json")
        self.play_state_path = os.path.join(self.data_dir, "play_state.json")
        self.diag_path = os.path.join(self.data_dir, "diag_log.json")
        self.cas_cache_path = os.path.join(self.data_dir, "cas_cache.json")
        self.log_path = os.path.join(self.data_dir, "139strm.log")
        self._lock = _CONFIG_LOCK
        self._cache = None
        self._last_error = ""

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    @property
    def last_error(self):
        return self._last_error

    def load_config(self):
        with self._lock:
            if self._cache is not None:
                return dict(self._cache)
            cfg = dict(DEFAULT_CONFIG)
            for path in (self.config_path, self.config_path + ".bak"):
                if not os.path.exists(path):
                    continue
                try:
                    raw = _read_json(path)
                    if isinstance(raw, dict):
                        cfg.update({k: v for k, v in raw.items() if k in DEFAULT_CONFIG})
                        if path != self.config_path:
                            self._last_error = f"主配置文件损坏，已从备份恢复（{path}）"
                        break
                except (OSError, ValueError) as exc:
                    self._last_error = f"读取 {path} 失败: {exc}"
                    self._quarantine(path)
            env_auth = os.environ.get("YUN139_AUTHORIZATION")
            if env_auth:
                cfg["authorization"] = env_auth
            if os.environ.get("YUN139_CLOUD_TYPE"):
                cfg["cloud_type"] = os.environ["YUN139_CLOUD_TYPE"]
            if os.environ.get("YUN139_OUTPUT_DIR"):
                cfg["output_dir"] = os.environ["YUN139_OUTPUT_DIR"]
            cfg = self._sanitize(cfg)
            self._cache = cfg
            return dict(cfg)

    def _quarantine(self, path):
        try:
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
            os.replace(path, f"{path}.corrupt-{stamp}")
        except OSError:
            pass

    @staticmethod
    def _sanitize(cfg):
        out = dict(cfg)
        out["authorization"] = str(out.get("authorization") or "").strip()
        if out.get("client_type") not in ("pc", "web"):
            out["client_type"] = "pc"
        for k in ("cas_enabled", "cas_allow_all_ext", "url_encode", "play_proxy",
                  "pc_full_headers"):
            out[k] = bool(out.get(k))
        try:
            out["cas_temp_ttl"] = max(30, int(out.get("cas_temp_ttl") or DEFAULT_TEMP_TTL))
        except (TypeError, ValueError):
            out["cas_temp_ttl"] = DEFAULT_TEMP_TTL
        try:
            out["min_size_mb"] = max(0, float(out.get("min_size_mb") or 0))
        except (TypeError, ValueError):
            out["min_size_mb"] = 0
        for k, fallback in (("media_ext", DEFAULT_MEDIA_EXT), ("copy_ext", DEFAULT_COPY_EXT)):
            v = out.get(k)
            if isinstance(v, str):
                v = [x.strip() for x in v.replace("，", ",").split(",") if x.strip()]
            if not isinstance(v, list) or not v:
                v = list(fallback)
            out[k] = [str(x).lower().lstrip(".") for x in v if str(x).strip()]
        out["base_url"] = str(out.get("base_url") or "").rstrip("/")
        out["output_dir"] = str(out.get("output_dir") or "/strm")
        return out

    def save_config(self, cfg):
        with self._lock:
            merged = dict(DEFAULT_CONFIG)
            merged.update({k: v for k, v in (cfg or {}).items() if k in DEFAULT_CONFIG})
            merged = self._sanitize(merged)
            os.makedirs(self.data_dir, exist_ok=True)
            written = []
            try:
                if os.path.exists(self.config_path):
                    # 只有旧文件能读通，才拿它当备份，避免把坏文件变成唯一备份
                    _atomic_write_json(self.config_path + ".bak",
                                       _read_json(self.config_path))
                    written.append(self.config_path + ".bak")
                _atomic_write_json(self.config_path, merged)
                written.append(self.config_path)
            except OSError:
                # 落盘失败时把写了一半的文件清掉，保证下次启动还能从 .bak 恢复
                for path in written:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                raise
            self._cache = merged
            self._last_error = ""
            return dict(merged)

    def update_config(self, patch):
        """局部更新：只改传入的字段，其余保持。"""
        cfg = self.load_config()
        cfg.update({k: v for k, v in (patch or {}).items() if k in DEFAULT_CONFIG})
        return self.save_config(cfg)

    # ------------------------------------------------------------------
    # 任务
    # ------------------------------------------------------------------

    def load_tasks(self):
        with self._lock:
            if not os.path.exists(self.tasks_path):
                return []
            try:
                data = _read_json(self.tasks_path)
                if isinstance(data, dict):
                    data = data.get("tasks")
                return [t for t in (data or []) if isinstance(t, dict)]
            except (OSError, ValueError):
                bak = self.tasks_path + ".bak"
                try:
                    data = _read_json(bak)
                    return [t for t in (data or []) if isinstance(t, dict)]
                except (OSError, ValueError):
                    self._quarantine(self.tasks_path)
                    return []

    def save_tasks(self, tasks):
        with self._lock:
            os.makedirs(self.data_dir, exist_ok=True)
            if os.path.exists(self.tasks_path):
                try:
                    _atomic_write_json(self.tasks_path + ".bak", _read_json(self.tasks_path))
                except (OSError, ValueError):
                    pass
            _atomic_write_json(self.tasks_path, list(tasks or []))

    # ------------------------------------------------------------------
    # 通用小文件（播放状态 / 诊断）读写
    # ------------------------------------------------------------------

    def read_json(self, path, default=None):
        try:
            if os.path.exists(path):
                return _read_json(path)
        except (OSError, ValueError):
            pass
        return default

    def write_json(self, path, data):
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            _atomic_write_json(path, data)
            return True
        except OSError:
            return False


def new_task_id():
    return uuid.uuid4().hex[:12]


def normalize_task(task, cfg=None):
    """把界面传来的任务字段规范化，返回干净的 dict（会补 id / 时间戳）。"""
    cfg = cfg or {}
    out = {
        "id": str(task.get("id") or new_task_id()),
        "name": str(task.get("name") or "").strip() or "task",
        "folder_id": str(task.get("folder_id") or "/"),
        "folder_path": str(task.get("folder_path") or "/"),
        "cron": str(task.get("cron") or "").strip(),
        "sync_mode": "force" if task.get("sync_mode") == "force" else "incremental",
        "delete_orphans": bool(task.get("delete_orphans")),
        "enabled": task.get("enabled", True) is not False,
        "media_ext": str(task.get("media_ext") or "").strip(),
        "copy_ext": str(task.get("copy_ext") or "").strip(),
        "min_size_mb": task.get("min_size_mb", ""),
        "last_run": task.get("last_run") or "",
        "last_result": task.get("last_result") or "",
        "last_stats": task.get("last_stats") or {},
        "created_at": task.get("created_at") or datetime.now().isoformat(timespec="seconds"),
    }
    # 强同步默认同时开启「删除孤儿」（与 README 的默认一致，但以界面显式值为准）
    if "delete_orphans" not in task:
        out["delete_orphans"] = out["sync_mode"] == "force"
    if not out["name"]:
        out["name"] = "task-" + str(int(time.time()))
    return out
