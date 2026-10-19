# -*- coding: utf-8 -*-
"""139Strm Pro —— 移动云盘（139 / 和彩云）STRM 生成器 + 302 直链服务。

设计要点：

* 只做移动云盘，直连官方接口，不依赖 OpenList、不需要境外中转；
* 生成的 ``.strm`` 内容是 ``<基础地址>/d/<file_id>``，播放时服务端换直链后
  302 跳转，视频流**不经过本机**；
* ``.cas`` 秒传文件在播放时还原成真文件再跳转，不占空间、不耗上传流量；
* 个人云接口使用 PC 客户端身份（``X-Yun-Client-Info`` 类型 13），单文件上限 500GB；
* 任务化：每个任务 = 一个云盘目录 + 增量/强同步 + 独立 crontab + 可强制终止。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime

try:      # 保留 try 只为在缺依赖时给出更友好的报错
    from flask import (Flask, Response, jsonify, redirect, render_template,
                       request)
except ImportError as exc:      # pragma: no cover
    raise SystemExit("缺少依赖，请先执行: pip install -r requirements.txt") from exc

from yun139 import __version__, cron
from yun139.cas import CASError, CASRestorer, fmt_size, is_cas_name
from yun139.client import (CLOUD_TYPES, count_reset, count_snapshot,
                           set_request_deadline, clear_request_deadline,
                           Yun139Client)
from yun139.errors import AuthError, NotFoundError, Yun139Error
from yun139.probe import LinkProbe
from yun139.store import Store, new_task_id, normalize_task
from yun139.strm import CancelError, DEFAULT_COPY_EXT, DEFAULT_MEDIA_EXT, StrmGenerator

logger = logging.getLogger("139strm")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORE = Store()

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))
app.config["JSON_AS_ASCII"] = False
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------
LINK_TTL_DEFAULT = 15 * 60          # 直链兜底寿命（实测约 900 秒）
CAS_LINK_MAX_TTL = 12 * 60          # 交出去的 CAS 链最多缓存 12 分钟（留 3 分钟余量）
CAS_LINK_MIN_REMAIN = 5 * 60        # 少于这个余命宁可换链
CAS_TEMP_GRACE = 120                # 链失效后临时文件再留 2 分钟
CAS_TEMP_MIN_TTL = 30 * 60          # 临时文件最短寿命
PLAY_REQUEST_DEADLINE = 25          # 一次播放请求里跟云盘打交道的时间上限
CLIENT_TTL = 3600
SCHEDULER_TICK = 20                 # 调度器扫描间隔（秒）
LAST_SEEN_KEEP = 300
RESUME_IDLE_GAP = 120               # 隔这么久再播 = 续播
DIAG_MAX = 60

PLAYER_EXTRA_HEADERS = {
    "Access-Control-Allow-Origin": "*",
}


# ----------------------------------------------------------------------
# 客户端缓存
# ----------------------------------------------------------------------
_clients = {}
_clients_lock = threading.Lock()
_clients_init_lock = threading.Lock()

_restorers = {}
_restorer_lock = threading.Lock()

_probe = LinkProbe()
_probe_lock = threading.Lock()
_probe_at = {}          # key -> (时刻, 结果)
_probe_suspect = {}     # key -> 连续「说不清」次数
CAS_PROBE_INTERVAL = 60
VERIFY_COOLDOWN = 60
_verify_retried = {}
_verify_lock = threading.Lock()


def load_config():
    return STORE.load_config()


def build_client(cfg=None, on_refresh=True):
    cfg = cfg or load_config()

    def _persist(new_auth):
        try:
            cfg2 = STORE.load_config()
            if cfg2.get("authorization") != new_auth:
                cfg2["authorization"] = new_auth
                STORE.save_config(cfg2)
                logger.info("令牌已续期并写回配置")
        except Exception as exc:      # noqa: BLE001
            logger.warning("写回续期令牌失败: %s", exc)

    return Yun139Client(
        authorization=cfg.get("authorization", ""),
        cloud_type=cfg.get("cloud_type", "personal_new"),
        mail_cookies=cfg.get("mail_cookies", ""),
        username=cfg.get("username", ""),
        cloud_id=cfg.get("cloud_id", ""),
        client_type=cfg.get("client_type", "pc"),
        pc_full_headers=cfg.get("pc_full_headers", False),
        on_token_refresh=_persist if on_refresh else None,
    )


def _client_key(cfg):
    return "|".join(str(cfg.get(k, "")) for k in
                    ("authorization", "cloud_type", "cloud_id", "username",
                     "client_type", "pc_full_headers"))


def get_client(cfg):
    """取缓存好的 client（并发请求只初始化一次）。"""
    key = _client_key(cfg)
    now = time.time()
    with _clients_lock:
        entry = _clients.get(key)
        if entry and now - entry[1] < CLIENT_TTL:
            return entry[0]
    with _clients_init_lock:
        with _clients_lock:
            entry = _clients.get(key)
            if entry and time.time() - entry[1] < CLIENT_TTL:
                return entry[0]
        client = build_client(cfg)
        client.init()
        with _clients_lock:
            _clients[key] = (client, time.time())
            for k in [k for k in _clients if k != key]:
                _clients.pop(k, None)
        return client


def drop_client(cfg=None):
    cfg = cfg or load_config()
    with _clients_lock:
        _clients.pop(_client_key(cfg), None)


def get_restorer(cfg, client):
    key = cfg.get("authorization", "")
    with _restorer_lock:
        rest = _restorers.get(key)
        if rest is None:
            rest = CASRestorer(client, cache_file=STORE.cas_cache_path)
            _restorers[key] = rest
        rest.client = client
        rest.temp_ttl = int(cfg.get("cas_temp_ttl") or 300)
        rest.allow_all_ext = bool(cfg.get("cas_allow_all_ext"))
        rest.set_temp_dir(cfg.get("cas_temp_dir_id") or "")
        return rest


def remember_temp_dir(cfg, dir_id):
    if dir_id and cfg.get("cas_temp_dir_id") != dir_id:
        try:
            STORE.update_config({"cas_temp_dir_id": dir_id})
        except OSError:
            pass


# ----------------------------------------------------------------------
# 直链寿命 / 缓存
# ----------------------------------------------------------------------
def amz_deadline(url):
    """从预签名直链读真正的过期时刻（``X-Amz-Date`` + ``X-Amz-Expires``）。"""
    try:
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(url).query)
        date_s = (q.get("X-Amz-Date") or [""])[0]
        exp_s = (q.get("X-Amz-Expires") or [""])[0]
        if date_s and exp_s:
            signed_at = time.mktime(time.strptime(date_s, "%Y%m%dT%H%M%SZ")) - time.timezone
            return signed_at + int(exp_s)
    except Exception:      # noqa: BLE001
        pass
    return None


def link_expire(url, default_ttl, max_ttl=None):
    """按直链自己的过期时间决定缓存多久。"""
    now = time.time()
    fallback = now + default_ttl
    if max_ttl:
        fallback = min(fallback, now + max_ttl)
    deadline = amz_deadline(url)
    if deadline and now - 86400 < deadline < now + 86400:
        expire = deadline - 60
        if max_ttl:
            expire = min(expire, now + max_ttl)
        return max(expire, now + 15)
    return fallback


_link_cache = {}
_cache_lock = threading.Lock()


def cache_get(key):
    with _cache_lock:
        return _link_cache.get(key)


def cache_put(key, value):
    with _cache_lock:
        if len(_link_cache) > 5000:
            now = time.time()
            for k in [k for k, v in _link_cache.items() if v[1] < now]:
                _link_cache.pop(k, None)
        _link_cache[key] = value


def cache_pop(key):
    with _cache_lock:
        return _link_cache.pop(key, None)


# ----------------------------------------------------------------------
# 探测节流
# ----------------------------------------------------------------------
def link_dead(key, url):
    """探一次链是否还活着；结果缓存 CAS_PROBE_INTERVAL 秒。"""
    if not url:
        return True
    now = time.time()
    with _probe_lock:
        hit = _probe_at.get(key)
    if hit and now - hit[0] < CAS_PROBE_INTERVAL:
        return hit[1] is False
    if not _probe.usable():
        return False
    res = _probe.probe(url)
    if res is None:
        with _probe_lock:
            n = _probe_suspect.get(key, 0) + 1
            _probe_suspect[key] = n
        _probe.note_unreachable()
        # 一次说不清可能是 CDN 正在回源；连续两次才敢判死
        dead = n >= 2
    else:
        with _probe_lock:
            _probe_suspect.pop(key, None)
        _probe.note_ok()
        dead = not res
    with _probe_lock:
        _probe_at[key] = (time.time(), not dead)
    return dead


def verify_retry_allowed(key):
    now = time.time()
    with _verify_lock:
        last = _verify_retried.get(key, 0)
        if now - last < VERIFY_COOLDOWN:
            return False
        _verify_retried[key] = now
        return True


def reset_link_state(key):
    with _probe_lock:
        _probe_at.pop(key, None)
        _probe_suspect.pop(key, None)
    with _verify_lock:
        _verify_retried.pop(key, None)


# ----------------------------------------------------------------------
# 播放记录（用于续播判定 + 诊断）
# ----------------------------------------------------------------------
_play_lock = threading.Lock()
_last_seen = {}
_last_seen_loaded = False
_last_seen_dirty = False
_last_seen_saved = 0.0


def play_state_load():
    global _last_seen_loaded
    if _last_seen_loaded:
        return
    _last_seen_loaded = True
    raw = STORE.read_json(STORE.play_state_path, {})
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                _last_seen[k] = float(v)
            except (TypeError, ValueError):
                continue


def play_state_save(force=False):
    global _last_seen_saved, _last_seen_dirty
    if not _last_seen_dirty:
        return
    if not force and time.time() - _last_seen_saved < 10:
        return
    with _play_lock:
        snapshot = dict(_last_seen)
        _last_seen_dirty = False
    _last_seen_saved = time.time()
    STORE.write_json(STORE.play_state_path, snapshot)


def mark_seen(file_id):
    """记录这次播放请求，返回与上一次的间隔（秒，首次为 0）。"""
    play_state_load()
    now = time.time()
    with _play_lock:
        prev = _last_seen.get(file_id)
        gap = int(now - prev) if prev else 0
        _last_seen[file_id] = now
        global _last_seen_dirty
        _last_seen_dirty = True
        if len(_last_seen) > LAST_SEEN_KEEP:
            keep = sorted(_last_seen.items(), key=lambda kv: kv[1],
                          reverse=True)[:LAST_SEEN_KEEP]
            _last_seen.clear()
            _last_seen.update(keep)
    play_state_save()
    return gap


# ----------------------------------------------------------------------
# 播放诊断
# ----------------------------------------------------------------------
_diag = []
_diag_lock = threading.Lock()
_diag_loaded = False
_diag_saved = 0.0


def diag_load():
    global _diag_loaded
    if _diag_loaded:
        return
    _diag_loaded = True
    raw = STORE.read_json(STORE.diag_path, {})
    items = raw.get("items") if isinstance(raw, dict) else None
    for it in (items or [])[-DIAG_MAX:]:
        if isinstance(it, dict):
            _diag.append(it)


def diag_add(**kw):
    global _diag_saved
    diag_load()
    kw["t"] = datetime.now().strftime("%m-%d %H:%M:%S")
    with _diag_lock:
        _diag.append(kw)
        if len(_diag) > DIAG_MAX:
            del _diag[:len(_diag) - DIAG_MAX]
    if time.time() - _diag_saved >= 10:
        _diag_saved = time.time()
        with _diag_lock:
            snapshot = list(_diag)
        STORE.write_json(STORE.diag_path, {"items": snapshot})


_play_meta = threading.local()
_known_size = {}
_known_size_lock = threading.Lock()


_piece_locks = {}
_piece_locks_lock = threading.Lock()


def play_lock(key):
    """取（必要时创建）同一部片子的串行锁。

    播放器会**同时**发好几条请求播同一个视频；按片子串行后，
    只有第一条去取链/还原，其余命中缓存秒回，不会各干一遍。
    """
    with _piece_locks_lock:
        lk = _piece_locks.get(key)
        if lk is None:
            if len(_piece_locks) > 500:
                _piece_locks.clear()
            lk = _piece_locks[key] = threading.Lock()
        return lk


# ----------------------------------------------------------------------
# 取链接
# ----------------------------------------------------------------------
def get_plain_link(client, file_id):
    now = time.time()
    cached = cache_get(file_id)
    if cached:
        url, expire = cached[0], cached[1]
        if expire - now >= 60 and not link_dead(file_id, url):
            return url
        if expire > now and not link_dead(file_id, url):
            return url
    url = client.get_download_url(file_id)
    cache_put(file_id, (url, link_expire(url, LINK_TTL_DEFAULT, CAS_LINK_MAX_TTL)))
    return url


def get_cas_link(client, cfg, file_id, cas_name, resume=False):
    with play_lock("cas:" + file_id):
        return _get_cas_link_inner(client, cfg, file_id, cas_name, resume)


def _get_cas_link_inner(client, cfg, file_id, cas_name, resume=False):
    """``.cas`` 的播放直链：秒传还原出临时文件，再把它的直链交给播放器。"""
    now = time.time()
    key = "cas:" + file_id
    restorer = get_restorer(cfg, client)

    if resume:
        # 续播：只丢弃「探测状态」，**不丢链接、不动会话登记表**。
        # 丢链接会让播放器手上的旧链与服务端不一致；丢会话登记会让清扫把
        # 播放器还在用的文件删掉 —— 两者都会表现成「跳到 0 秒」。
        reset_link_state(key)

    cached = cache_get(key)
    if cached and len(cached) >= 3:
        url, expire, temp_id = cached[0], cached[1], cached[2]
        remain = expire - now
        dead = link_dead(key, url)
        if remain >= CAS_LINK_MIN_REMAIN and not dead:
            restorer.schedule_delete(temp_id, delay=_cas_temp_delay(restorer, now, expire))
            return url
        if not dead:
            # 链接快到期 → 对**同一个文件**重签一条，文件原地不动
            fresh = restorer.refresh_link(temp_id)
            if fresh:
                expire2 = link_expire(fresh, LINK_TTL_DEFAULT, CAS_LINK_MAX_TTL)
                restorer.schedule_delete(temp_id,
                                         delay=_cas_temp_delay(restorer, now, expire2))
                cache_put(key, (fresh, expire2, temp_id))
                _play_meta.restored = False
                return fresh
        if verify_retry_allowed(key):
            cache_pop(key)
        else:
            # 判定失效但冷却中：先复用旧链，避免还原风暴
            restorer.schedule_delete(temp_id, delay=_cas_temp_delay(restorer, now, expire))
            return url

    url, size, temp_id, real_name, restored = restorer.fetch_link(file_id, cas_name)
    _play_meta.restored = bool(restored)
    if size:
        _play_meta.size = int(size)
        with _known_size_lock:
            _known_size[file_id] = int(size)
    if _fresh_link_broken(key, url, cas_name):
        logger.warning("刚取到的 CAS 直链不可用（%s），删除后重新还原一次", cas_name)
        restorer.delete_quietly(temp_id)
        url, size, temp_id, real_name, restored = restorer.fetch_link(file_id, cas_name)
    expire = link_expire(url, LINK_TTL_DEFAULT, CAS_LINK_MAX_TTL)
    restorer.schedule_delete(temp_id, delay=_cas_temp_delay(restorer, now, expire))
    if restored:
        remember_temp_dir(cfg, restorer.get_temp_dir())
        logger.info("CAS 还原成功 %s -> %s (%s)", cas_name, real_name, fmt_size(size))
    cache_put(key, (url, expire, temp_id))
    return url


def _cas_temp_delay(restorer, now, expire):
    return max(int(restorer.temp_ttl or 0), int(expire - now + CAS_TEMP_GRACE),
               CAS_TEMP_MIN_TTL)


def _fresh_link_broken(key, url, cas_name=""):
    if not _probe.usable():
        return False
    if _probe.probe(url) is not False:
        _probe.note_ok()
        return False
    if _probe.note_fresh_dead():
        logger.warning("连续多条刚还原的链都被判坏，熔断 %s 秒：%s",
                       _probe.status()["breaker_left"], cas_name)
        return False
    return True


# ----------------------------------------------------------------------
# 错误翻译
# ----------------------------------------------------------------------
_CAS_FATAL_MARKS = ("权益不足", "不是视频", "已失效无法还原", "SHA256",
                    "秒传失败", "请手动删除", "500GB", "只有新版个人云")


def cas_error_is_final(exc):
    if not isinstance(exc, CASError):
        return False
    return any(m in str(exc) for m in _CAS_FATAL_MARKS)


def friendly_error(exc):
    """把底层异常翻成人话，直接显示给播放器。"""
    msg = str(exc)
    low = msg.lower()
    if isinstance(exc, AuthError) or ("authorization" in low and
                                     ("过期" in msg or "expired" in low)):
        return ("令牌不可用：%s\n请到「系统设置」重新获取 Authorization"
                "（或配置邮箱 Cookie 自动登录）。" % msg[:200])
    if "刷新令牌" in msg:
        return f"令牌续期接口返回异常：{msg[:160]}（令牌本身可能仍可用，请稍后重试）"
    if "bad gateway" in low or "502" in msg or "503" in msg:
        return "获取直链失败：移动云盘接口暂时不可用（502/503）。请稍等几秒重试。"
    if "timed out" in low or "timeout" in low or "等待上限" in msg:
        return "获取直链失败：移动云盘接口响应太慢，已超过等待上限。请重试（重试会快很多）。"
    if "connection" in low or "max retries" in low:
        return "获取直链失败：连不上移动云盘。请检查服务器网络后重试。"
    if isinstance(exc, CASError):
        return "CAS 秒传还原失败：" + msg[:300]
    return f"获取直链失败: {msg[:300]}"


def _error_response(msg, status=502):
    resp = Response(msg + "\n", status=status, mimetype="text/plain; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def resolve_play_url(cfg, file_id, cas_name, use_cas, resume=False):
    """换取一条可用直链；成功返回 ``(url, None)``，失败返回 ``(None, Response)``。"""
    url, last_exc = None, None
    for attempt in (1, 2):
        try:
            if attempt == 1:
                client = get_client(cfg)
            else:
                drop_client(cfg)
                client = build_client(cfg)
                client.init()
            url = (get_cas_link(client, cfg, file_id, cas_name, resume)
                   if use_cas else get_plain_link(client, file_id))
            return url, None
        except (Yun139Error, CASError) as exc:
            last_exc = exc
            logger.info("取链失败（第 %d 次）: %s", attempt, exc)
            if attempt == 2 or cas_error_is_final(exc):
                break
        except Exception as exc:      # noqa: BLE001
            last_exc = exc
            logger.exception("取链出现未预期错误")
            break
    status = 404 if isinstance(last_exc, NotFoundError) else (
        503 if isinstance(last_exc, AuthError) else 502)
    return None, _error_response(friendly_error(last_exc), status)


# ----------------------------------------------------------------------
# 播放路由
# ----------------------------------------------------------------------
def _default_base_url():
    """没配置基础地址时，用本机回环 + 端口拼一个（Windows 上 env 名不区分大小写，
    不能用 ``os.environ.get("PORT")`` 之外的名字，否则会和界面的 PORT 变量串味）。"""
    port = os.environ.get("PORT") or "8025"
    return "http://127.0.0.1:%s" % port


def _run_base_url(cfg):
    """任务生成 strm 时使用的基础地址（不依赖请求上下文）。"""
    base = (cfg.get("base_url") or "").strip().rstrip("/")
    if not base:
        logger.warning("未设置「基础地址」，临时使用 %s；请在系统设置里改成播放器可访问的地址",
                       _default_base_url())
        base = _default_base_url()
    return base


@app.route("/")
def index():
    cfg = load_config()
    return render_template("index.html", version=__version__,
                           site_title=cfg.get("site_title") or "139Strm Pro")


@app.route("/health")
def health():
    return jsonify({"ok": True, "version": __version__,
                    "tasks": len(STORE.load_tasks())})


@app.route("/d/<path:file_id>", methods=["GET", "HEAD", "OPTIONS"])
def direct_link(file_id):
    """播放器请求这里，换取移动云盘直链并 302 跳转（或代理转发）。"""
    if request.method == "OPTIONS":
        return Response(status=204, headers=PLAYER_EXTRA_HEADERS)

    cfg = load_config()
    if not cfg.get("authorization") and not cfg.get("mail_cookies"):
        return _error_response("尚未配置移动云盘 Authorization，请先到「系统设置」填写。", 503)

    cas_name = request.args.get("cas") or ""
    use_cas = bool(cas_name) and is_cas_name(cas_name) and cfg.get("cas_enabled", True)
    if cfg.get("play_proxy") or request.args.get("proxy"):
        return proxy_stream(cfg, file_id, cas_name, use_cas)

    t0 = time.time()
    gap = mark_seen(file_id)
    resume = bool(gap) and gap > RESUME_IDLE_GAP
    set_request_deadline(PLAY_REQUEST_DEADLINE)
    count_reset()
    _play_meta.restored = None
    try:
        url, err = resolve_play_url(cfg, file_id, cas_name, use_cas, resume)
    finally:
        clear_request_deadline()
    calls, call_ms = count_snapshot()

    if url is None:
        diag_add(ok=False, name=cas_name or file_id[:12], cas=use_cas,
                 ms=int((time.time() - t0) * 1000), resume=resume, gap=gap,
                 calls=calls, call_ms=call_ms, err=(err.get_data(as_text=True) or "")[:160])
        return err

    life = int(link_expire(url, LINK_TTL_DEFAULT, CAS_LINK_MAX_TTL) - time.time())
    diag_add(ok=True, name=cas_name or file_id[:12], cas=use_cas,
             ms=int((time.time() - t0) * 1000), life=max(life, 0),
             resume=resume, gap=gap, calls=calls, call_ms=call_ms,
             newfile=getattr(_play_meta, "restored", None),
             rng=(request.headers.get("Range") or "")[:40])
    resp = redirect(url, code=302)
    resp.headers.update(PLAYER_EXTRA_HEADERS)
    # 防缓存头给全：任何一层缓存了这个 302，之后就会一直用那条会过期的直链
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.headers["Accept-Ranges"] = "bytes"
    return resp


def proxy_stream(cfg, file_id, cas_name, use_cas):
    """代理转发：视频流经本机中转，直链过期可随时换（只适合内网/家宽）。"""
    import requests as rq

    gap = mark_seen(file_id)
    resume = bool(gap) and gap > RESUME_IDLE_GAP
    set_request_deadline(PLAY_REQUEST_DEADLINE)
    try:
        url, err = resolve_play_url(cfg, file_id, cas_name, use_cas, resume)
    finally:
        clear_request_deadline()
    if url is None:
        return err

    def _open(u):
        rng = request.headers.get("Range")
        headers = {"Range": rng} if (rng and request.method != "HEAD") else {}
        return rq.get(u, headers=headers, stream=True, timeout=(15, 60))

    up = _open(url)
    if up.status_code >= 400:
        logger.info("代理上游返回 %d，换新链重试", up.status_code)
        up.close()
        key = ("cas:" + file_id) if use_cas else file_id
        cache_pop(key)
        url, err = resolve_play_url(cfg, file_id, cas_name, use_cas, True)
        if url is None:
            return err
        up = _open(url)

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": up.headers.get("Content-Type") or "application/octet-stream",
        "Cache-Control": "no-store",
    }
    for h in ("Content-Length", "Content-Range", "ETag", "Last-Modified"):
        if h in up.headers:
            headers[h] = up.headers[h]
    headers.update(PLAYER_EXTRA_HEADERS)
    if request.method == "HEAD":
        up.close()
        return Response(status=up.status_code, headers=headers)

    def _gen():
        try:
            for chunk in up.iter_content(chunk_size=256 * 1024):
                if chunk:
                    yield chunk
        finally:
            up.close()

    return Response(_gen(), status=up.status_code, headers=headers)


# ----------------------------------------------------------------------
# 配置 / 云盘接口
# ----------------------------------------------------------------------
@app.get("/api/config")
def api_get_config():
    cfg = load_config()
    safe = dict(cfg)
    auth = safe.get("authorization") or ""
    # 凭据绝不回显给浏览器：只给掩码 / 是否已设置 / 有效期
    safe.pop("authorization", None)
    safe["authorization_masked"] = ("*" * 12 + auth[-8:]) if len(auth) > 20 else ("已设置" if auth else "")
    safe["has_authorization"] = bool(auth)
    try:
        client = build_client(cfg, on_refresh=False) if auth else None
        expire = client.get_expire_time() if client else None
        safe["token_expire"] = expire.strftime("%Y-%m-%d %H:%M") if expire else ""
    except Yun139Error:
        safe["token_expire"] = ""
    # 邮箱 Cookie 同样是机密，只回显是否存在
    safe["has_mail_cookies"] = bool(safe.get("mail_cookies"))
    safe.pop("mail_cookies", None)
    safe["store_error"] = STORE.last_error
    safe["version"] = __version__
    safe["cloud_types"] = list(CLOUD_TYPES)
    return jsonify(safe)


@app.post("/api/config")
def api_save_config():
    data = dict(request.get_json(force=True, silent=True) or {})
    # 掩码值不覆盖真实凭据
    if isinstance(data.get("authorization"), str):
        val = data["authorization"].strip()
        if val.startswith("*") or val == "已设置":
            data.pop("authorization")
        elif val.lower().startswith("basic "):
            data["authorization"] = val[6:].strip()
    # 邮箱 Cookie 同理：留空 = 不修改；填 "-" = 清空
    if isinstance(data.get("mail_cookies"), str):
        val = data["mail_cookies"].strip()
        if val == "":
            data.pop("mail_cookies")
        elif val == "-":
            data["mail_cookies"] = ""
    try:
        cfg = STORE.update_config(data)
    except OSError as exc:
        return jsonify({"ok": False, "error": f"写入配置失败: {exc}"}), 500
    drop_client(cfg)
    return jsonify({"ok": True})


@app.post("/api/test")
def api_test():
    cfg = load_config()
    try:
        client = build_client(cfg)
        client.init()
        items = client.list_files(client.root_folder_id)
        expire = client.get_expire_time()
        return jsonify({
            "ok": True,
            "account": client.account or client.account_name(),
            "host": client.personal_host,
            "root_items": len(items),
            "folders": sum(1 for i in items if i.is_folder),
            "files": sum(1 for i in items if not i.is_folder),
            "client_type": client.client_type,
            "single_file_limit": "500GB" if client.client_type == "pc" else "50GB",
            "token_expire": expire.strftime("%Y-%m-%d %H:%M") if expire else "",
        })
    except Yun139Error as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:      # noqa: BLE001
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


@app.get("/api/debug/list")
def api_debug_list():
    """目录列表诊断：把「发出去的请求」和「云盘原样返回」都摆出来。

    排查「资源不存在」这类问题时，先看这里：
      * init 是否拿到接入点、account 是否解出来了
      * 实际发往 /file/list 的 URL 和 body
      * 云盘的原始响应（success / message / 条目数）
    """
    cfg = load_config()
    folder = request.args.get("folder") or "/"
    out = {"ok": True, "folder": folder}
    try:
        client = get_client(cfg)
        out["account"] = client.account or client.account_name()
        out["host"] = client.personal_host
        out["cloud_type"] = client.cloud_type
        out["client_type"] = client.client_type
        out["root_folder_id"] = client.root_folder_id
    except Exception as exc:      # noqa: BLE001
        out.update(ok=False, stage="init", error=f"{type(exc).__name__}: {exc}")
        return jsonify(out), 400

    target = folder if folder not in ("", "root") else client.root_folder_id
    payload = {
        "imageThumbnailStyleList": ["Small", "Large"],
        "orderBy": "updated_at",
        "orderDirection": "DESC",
        "pageInfo": {"pageCursor": "", "pageSize": 100},
        "parentFileId": target,
    }
    out["request_url"] = client.personal_host + "/file/list"
    out["request_body"] = payload
    out["account_info_sent"] = client._common_account_info()
    try:
        resp = client._personal_post("/file/list", payload)
        data = resp.get("data") or {}
        items = data.get("items") or data.get("fileList") or []
        out["raw_keys"] = sorted(resp.keys())
        out["success"] = resp.get("success")
        out["message"] = resp.get("message") or resp.get("msg") or ""
        out["item_count"] = len(items)
        out["items_sample"] = [
            {"name": i.get("name"), "type": i.get("type"),
             "size": i.get("size"), "id": (i.get("fileId") or "")[:12]}
            for i in items[:5]
        ]
        out["data_keys"] = sorted(data.keys()) if isinstance(data, dict) else str(type(data))
    except Yun139Error as exc:
        out.update(ok=False, stage="file/list", error=str(exc))
    except Exception as exc:      # noqa: BLE001
        out.update(ok=False, stage="file/list", error=f"{type(exc).__name__}: {exc}")
    return jsonify(out)


@app.get("/api/list")
def api_list():
    cfg = load_config()
    folder = (request.args.get("folder") or "/").strip()
    # 根目录用空串或 "/" 都归一到接入点自己的根 ID；
    # 其它情况必须是云盘返回的 fileId（形如 Fruo07iH...），不能是「/目录名」这种路径
    target = folder if folder not in ("", "root", "/") else None
    if target is not None and target.startswith("/"):
        return jsonify({
            "ok": False,
            "error": ("目录参数看起来是路径而不是 fileId。云盘只接受 fileId"
                      "（一串字母数字），请从「存储浏览」里逐级点击进入。"),
            "folder": folder,
        }), 400
    try:
        client = get_client(cfg)
        if target is None:
            target = client.root_folder_id
        items = client.list_files(target)
        items.sort(key=lambda i: (not i.is_folder, i.name.lower()))
        return jsonify({"ok": True, "items": [i.to_dict() for i in items],
                        "folder": folder, "folder_id": target})
    except Yun139Error as exc:
        logger.warning("列出目录失败 folder_id=%s: %s", target or folder, exc)
        return jsonify({"ok": False, "error": str(exc),
                        "folder": folder, "folder_id": target or ""}), 400
    except Exception as exc:      # noqa: BLE001
        logger.exception("列出目录出现未预期错误 folder_id=%s", target or folder)
        return jsonify({"ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                        "folder": folder}), 500


# ----------------------------------------------------------------------
# 任务
# ----------------------------------------------------------------------
_task_queue = []
_task_queue_lock = threading.Lock()
_task_running = threading.Event()
_runner_active = threading.Event()
_task_state = {
    "running": False,
    "task_id": "",
    "task_name": "",
    "started_at": 0,
    "phase": "",
    "scanned": 0,
    "done": 0,
}
_task_state_lock = threading.Lock()
_current_generator = {"gen": None}
_stop_requested = threading.Event()
_last_trigger = {}


def task_view(task):
    t = dict(task)
    t["next_run"] = ""
    if t.get("enabled") and t.get("cron"):
        nxt = cron.next_run_from_cron(t["cron"])
        t["next_run"] = nxt.strftime("%Y-%m-%d %H:%M") if nxt else "表达式无效"
    t["output_dir"] = os.path.join(load_config().get("output_dir", "/strm"), t["name"])
    return t


@app.get("/api/tasks")
def api_tasks():
    tasks = [task_view(t) for t in STORE.load_tasks()]
    return jsonify({"ok": True, "tasks": tasks,
                    "presets": [{"expr": e, "label": l} for e, l in cron.PRESETS]})


def _task_payload(data, base=None):
    merged = dict(base or {})
    for k in ("name", "folder_id", "folder_path", "cron", "sync_mode",
              "delete_orphans", "enabled", "media_ext", "copy_ext", "min_size_mb"):
        if k in data:
            merged[k] = data[k]
    if base is None:
        merged["id"] = new_task_id()
    else:
        merged["id"] = base["id"]
    return normalize_task(merged)


def _validate_task(task):
    if task.get("cron") and not cron.is_valid_cron(task["cron"]):
        return "crontab 表达式无效，应为 5 段：分 时 日 月 周"
    if str(task.get("name", "")).strip() in ("", ".", "..") or "/" in task.get("name", ""):
        return "任务名不能为空，且不能包含 /"
    return ""


@app.post("/api/tasks")
def api_task_create():
    data = request.get_json(force=True, silent=True) or {}
    task = _task_payload(data)
    err = _validate_task(task)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    tasks = STORE.load_tasks()
    if any(t.get("name") == task["name"] for t in tasks):
        return jsonify({"ok": False, "error": f"已存在同名任务「{task['name']}」"}), 400
    tasks.append(task)
    STORE.save_tasks(tasks)
    return jsonify({"ok": True, "task": task_view(task)})


@app.put("/api/tasks/<task_id>")
def api_task_update(task_id):
    data = request.get_json(force=True, silent=True) or {}
    tasks = STORE.load_tasks()
    for idx, t in enumerate(tasks):
        if t.get("id") == task_id:
            task = _task_payload(data, base=t)
            err = _validate_task(task)
            if err:
                return jsonify({"ok": False, "error": err}), 400
            tasks[idx] = task
            STORE.save_tasks(tasks)
            return jsonify({"ok": True, "task": task_view(task)})
    return jsonify({"ok": False, "error": "任务不存在"}), 404


@app.delete("/api/tasks/<task_id>")
def api_task_delete(task_id):
    clean = request.args.get("clean_output") == "1"
    tasks = STORE.load_tasks()
    target = next((t for t in tasks if t.get("id") == task_id), None)
    if not target:
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    tasks = [t for t in tasks if t.get("id") != task_id]
    STORE.save_tasks(tasks)
    removed = 0
    if clean:
        root = os.path.join(load_config().get("output_dir", "/strm"), target["name"])
        removed = _remove_tree(root)
    return jsonify({"ok": True, "cleaned": removed})


def _remove_tree(root):
    """只删除 ``*.strm``，再清空空目录；绝不删用户放进去的其他文件。"""
    if not root or not os.path.isdir(root):
        return 0
    n = 0
    for d, _subs, files in os.walk(root, topdown=False):
        for fn in files:
            if fn.lower().endswith(".strm"):
                try:
                    os.remove(os.path.join(d, fn))
                    n += 1
                except OSError:
                    pass
        try:
            if not os.listdir(d):
                os.rmdir(d)
        except OSError:
            pass
    return n


def _build_job(task, cfg):
    name = task["name"]
    output_dir = cfg.get("output_dir") or "/strm"
    media = task.get("media_ext") or ",".join(cfg.get("media_ext") or DEFAULT_MEDIA_EXT)
    copye = task.get("copy_ext") or ",".join(cfg.get("copy_ext") or DEFAULT_COPY_EXT)
    try:
        min_mb = float(task.get("min_size_mb")) if str(task.get("min_size_mb", "")) != "" \
            else float(cfg.get("min_size_mb") or 0)
    except (TypeError, ValueError):
        min_mb = 0.0
    return {
        "task_id": task["id"],
        "task_name": name,
        "folder_id": task.get("folder_id") or "/",
        "folder_path": task.get("folder_path") or "/",
        "output_dir": output_dir,
        "target_subdir": name,
        "force": task.get("sync_mode") == "force",
        "delete_orphans": bool(task.get("delete_orphans")),
        "media_ext": [x.strip().lower().lstrip(".") for x in str(media).split(",") if x.strip()],
        "copy_ext": [x.strip().lower() if x.strip().startswith(".") or True
                     else x for x in str(copye).split(",") if x.strip()],
        "min_size_mb": min_mb,
    }


def enqueue_jobs(jobs):
    with _task_queue_lock:
        _task_queue.extend(jobs)
    if not _runner_active.is_set():
        threading.Thread(target=_runner_loop, daemon=True, name="task-runner").start()


def _set_state(**kw):
    with _task_state_lock:
        _task_state.update(kw)


def runner_running():
    return _runner_active.is_set() or bool(_task_queue)


def _runner_loop():
    if _runner_active.is_set():
        return
    _runner_active.set()
    try:
        while True:
            with _task_queue_lock:
                if not _task_queue:
                    break
                job = _task_queue.pop(0)
            if _stop_requested.is_set():
                logger.info("已终止：跳过队列中的任务「%s」", job["task_name"])
                continue
            try:
                run_strm_job(job)
            except Exception:      # noqa: BLE001
                logger.exception("任务「%s」运行异常", job["task_name"])
    finally:
        _task_queue.clear()
        _runner_active.clear()
        _stop_requested.clear()
        _set_state(running=False, task_id="", phase="")


def run_strm_job(job):
    cfg = load_config()
    base = _run_base_url(cfg)
    logger.info("任务开始：%s（%s）", job["task_name"], job["folder_path"])
    _set_state(running=True, task_id=job["task_id"], task_name=job["task_name"],
               started_at=time.time(), phase="连接云盘", scanned=0, done=0)
    gen = None
    try:
        client = get_client(cfg)
        out = os.path.join(job["output_dir"], job["target_subdir"])
        os.makedirs(out, exist_ok=True)

        def _progress(scanned, done):
            _set_state(scanned=scanned, done=done, phase="扫描生成")

        gen = StrmGenerator(
            client=client, base_url=base, output_dir=job["output_dir"],
            media_ext=job["media_ext"], copy_ext=job["copy_ext"],
            min_size_mb=job["min_size_mb"], url_encode=cfg.get("url_encode", True),
            force=job["force"], delete_orphans=job["delete_orphans"],
            progress=_progress)
        _current_generator["gen"] = gen
        _set_state(phase="扫描生成")
        gen.generate(job["folder_id"], job["target_subdir"])
        summary = gen.summary()
        result = "成功"
    except CancelError:
        summary = gen.summary() if gen else {}
        summary["cancelled"] = True
        result = "已终止"
    except (Yun139Error, OSError, ValueError) as exc:
        summary = gen.summary() if gen else {}
        summary["errors"] = (summary.get("errors") or []) + [str(exc)]
        summary["error_count"] = len(summary["errors"])
        result = "失败"
        logger.warning("任务「%s」失败: %s", job["task_name"], exc)
    finally:
        _current_generator["gen"] = None

    summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
    tasks = STORE.load_tasks()
    for t in tasks:
        if t.get("id") == job["task_id"]:
            t["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            t["last_result"] = result
            t["last_stats"] = {k: v for k, v in summary.items() if k != "logs"}
            break
    STORE.save_tasks(tasks)
    logger.info("任务结束：%s -> %s（新建 %s / 更新 %s / 跳过 %s / 删除 %s / 错误 %s）",
                job["task_name"], result, summary.get("created"),
                summary.get("updated"), summary.get("skipped"),
                summary.get("deleted"), summary.get("error_count"))
    _set_state(running=False, task_id="", phase="")
    return summary


@app.post("/api/tasks/run")
def api_run_all():
    if runner_running():
        return jsonify({"ok": False, "error": "已有任务在运行"}), 409
    cfg = load_config()
    _stop_requested.clear()
    jobs = [_build_job(t, cfg) for t in STORE.load_tasks() if t.get("enabled", True)]
    if not jobs:
        return jsonify({"ok": False, "error": "没有已启用的任务"}), 400
    enqueue_jobs(jobs)
    return jsonify({"ok": True, "queued": len(jobs)})


@app.post("/api/tasks/<task_id>/run")
def api_run_one(task_id):
    if runner_running():
        return jsonify({"ok": False, "error": "已有任务在运行"}), 409
    task = next((t for t in STORE.load_tasks() if t.get("id") == task_id), None)
    if not task:
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    _stop_requested.clear()
    enqueue_jobs([_build_job(task, load_config())])
    return jsonify({"ok": True})


@app.post("/api/tasks/stop")
def api_stop():
    _stop_requested.set()
    with _task_queue_lock:
        dropped = len(_task_queue)
        _task_queue.clear()
    gen = _current_generator.get("gen")
    if gen:
        gen.cancel()
    return jsonify({"ok": True, "dropped": dropped, "cancelling": bool(gen)})


@app.get("/api/strm/status")
def api_status():
    with _task_state_lock:
        st = dict(_task_state)
    if st.get("started_at"):
        st["elapsed"] = int(time.time() - st["started_at"])
    gen = _current_generator.get("gen")
    st["logs"] = gen.log_lines[-60:] if gen else []
    with _task_queue_lock:
        st["queued"] = len(_task_queue)
    return jsonify(st)


def scheduler_loop():
    """每 SCHEDULER_TICK 秒扫一遍，按 crontab 触发到点的启用任务。"""
    while True:
        try:
            time.sleep(SCHEDULER_TICK)
            if runner_running():
                continue
            tick = datetime.now().replace(second=0, microsecond=0)
            due = []
            for t in STORE.load_tasks():
                if not t.get("enabled", True):
                    continue
                expr = (t.get("cron") or "").strip()
                if not expr:
                    continue
                if _last_trigger.get(t.get("id")) == tick:
                    continue
                if cron.cron_matches(expr, tick):
                    due.append(t)
                    _last_trigger[t.get("id")] = tick
            if due:
                cfg = load_config()
                _stop_requested.clear()
                enqueue_jobs([_build_job(t, cfg) for t in due])
        except Exception as exc:      # noqa: BLE001
            logger.warning("调度器异常: %s", exc)


# ----------------------------------------------------------------------
# CAS 管理
# ----------------------------------------------------------------------
@app.get("/api/cas/status")
def api_cas_status():
    cfg = load_config()
    with _restorer_lock:
        restorers = list(_restorers.values())
    out = {
        "ok": True,
        "enabled": bool(cfg.get("cas_enabled", True)),
        "temp_ttl": cfg.get("cas_temp_ttl", 300),
        "allow_all_ext": bool(cfg.get("cas_allow_all_ext", False)),
        "pending_cleanup": sum(r.pending_count() for r in restorers),
        "active_sessions": sum(r.session_count() for r in restorers),
        "client_type": cfg.get("client_type", "pc"),
        "single_file_limit": "500GB" if cfg.get("client_type", "pc") == "pc" else "50GB",
        "probe": _probe.status(),
    }
    if cfg.get("authorization") and request.args.get("scan") == "1":
        try:
            client = get_client(cfg)
            rest = get_restorer(cfg, client)
            items = client.list_files(rest.ensure_temp_dir())
            out["temp_files"] = len(items)
            out["temp_bytes"] = sum(i.size for i in items)
        except Exception as exc:      # noqa: BLE001
            out["temp_dir_error"] = str(exc)
    return jsonify(out)


@app.post("/api/cas/purge")
def api_cas_purge():
    cfg = load_config()
    data = request.get_json(force=True, silent=True) or {}
    try:
        client = get_client(cfg)
        rest = get_restorer(cfg, client)
        count = rest.purge_temp_dir(data.get("max_age"))
        return jsonify({"ok": True, "deleted": count})
    except Exception as exc:      # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500


# ----------------------------------------------------------------------
# 诊断 / 其它
# ----------------------------------------------------------------------
@app.get("/api/link/<path:file_id>")
def api_link(file_id):
    """调试用：直接看直链，不跳转。"""
    cfg = load_config()
    cas_name = request.args.get("cas") or ""
    use_cas = bool(cas_name) and is_cas_name(cas_name)
    set_request_deadline(PLAY_REQUEST_DEADLINE)
    try:
        url, err = resolve_play_url(cfg, file_id, cas_name, use_cas)
    finally:
        clear_request_deadline()
    if url is None:
        return err
    expire = int(link_expire(url, LINK_TTL_DEFAULT, CAS_LINK_MAX_TTL) - time.time())
    return jsonify({"ok": True, "url": url, "expire_in": max(expire, 0),
                    "probe": _probe.status()})


@app.get("/api/diag")
def api_diag():
    diag_load()
    with _diag_lock:
        items = list(_diag)
    return jsonify({"ok": True, "items": list(reversed(items)),
                    "probe": _probe.status()})


@app.post("/api/diag/clear")
def api_diag_clear():
    with _diag_lock:
        _diag.clear()
    STORE.write_json(STORE.diag_path, {"items": []})
    return jsonify({"ok": True})


@app.get("/api/storage")
def api_storage():
    cfg = load_config()
    rows = []
    for label, path in (("配置目录", STORE.data_dir),
                        ("STRM 目录", cfg.get("output_dir") or "/strm")):
        rows.append({
            "label": label,
            "path": path,
            "exists": os.path.isdir(path),
            "writable": os.access(path, os.W_OK) if os.path.isdir(path) else False,
            "mount": os.path.ismount(path) if hasattr(os.path, "ismount") else False,
        })
    return jsonify({"ok": True, "rows": rows, "config_path": STORE.config_path})


@app.get("/api/log")
def api_log():
    try:
        if not os.path.exists(STORE.log_path):
            return jsonify({"ok": True, "lines": []})
        with open(STORE.log_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-300:]
        return jsonify({"ok": True, "lines": [l.rstrip("\n") for l in lines]})
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


# ----------------------------------------------------------------------
# 启动
# ----------------------------------------------------------------------
def setup_logging():
    os.makedirs(STORE.data_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
        fh = logging.FileHandler(STORE.log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    root.setLevel(logging.INFO)


def start_background():
    setup_logging()
    cfg = load_config()
    logger.info("139Strm Pro %s 启动，数据目录 %s，STRM 目录 %s",
                __version__, STORE.data_dir, cfg.get("output_dir"))
    threading.Thread(target=scheduler_loop, daemon=True, name="scheduler").start()


def main():
    start_background()
    port = int(os.environ.get("PORT") or 8025)
    host = os.environ.get("BIND_ADDR") or "0.0.0.0"
    try:
        from waitress import serve
        logger.info("使用 waitress 监听 %s:%d（多线程）", host, port)
        serve(app, host=host, port=port, threads=16, channel_timeout=120)
    except ImportError:
        logger.info("未安装 waitress，改用 Flask 内置服务器")
        app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
