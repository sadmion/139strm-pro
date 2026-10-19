# -*- coding: utf-8 -*-
"""端到端冒烟测试：用本地 mock 云盘替换网络层，跑通「建任务 → 生成 STRM → 302 播放」全链路。

不依赖真实账号、不访问外网。运行：
    python tests/e2e_mock.py
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WORK = tempfile.mkdtemp(prefix="139strm-e2e-")
os.environ["CONFIG_PATH"] = os.path.join(WORK, "config.json")
os.makedirs(os.path.join(WORK, "strm"), exist_ok=True)

from yun139 import cas as cas_mod                      # noqa: E402
from yun139.client import FileItem                     # noqa: E402
from yun139.store import Store                         # noqa: E402

import app as appmod                                   # noqa: E402

appmod.STORE = Store(os.environ["CONFIG_PATH"])
appmod.STORE.update_config({"authorization": "dummy-token-for-mock",
                            "base_url": "http://player.local:8025",
                            "output_dir": os.path.join(WORK, "strm")})

VIDEO = b"\x1a\x45\xdf\xa3" + b"MOCKVIDEO" * 4000        # 假 MKV


class MockClient:
    """内存目录树 + 假 139 行为（秒传还原、直链、删除）。"""

    cloud_type = "personal_new"
    root_folder_id = "/"
    timeout = (2, 2)

    def __init__(self):
        self.tree = {"/": []}
        self.cas_blobs = {}
        self.dl_calls = 0
        self.restores = 0
        self._seq = 100
        self._build()

    def _fid(self, prefix="f"):
        self._seq += 1
        return f"{prefix}{self._seq}"

    def _add(self, parent, name, size, is_folder=False):
        fid = self._fid("d" if is_folder else "f")
        self.tree.setdefault(parent, []).append(
            FileItem(fid, name, size=size, is_folder=is_folder))
        if is_folder:
            self.tree.setdefault(fid, [])
        return fid

    def _build(self):
        movie = self._add("/", "movies", 0, True)
        tv = self._add("/", "tv", 0, True)
        self.movie_dir = movie
        self.tv_dir = tv
        self._add(movie, "普通电影.mkv", 8 * 1024 ** 3)
        self._add(movie, "普通电影.chs.srt", 2048)
        cas_info = {"provider": "139", "name": "秒传电影.mkv",
                    "size": 120 * 1024 ** 3, "md5": "a" * 32,
                    "sha256": "c" * 64, "sliceMd5": "d" * 32}
        cas_fid = self._add(movie, "秒传电影.mkv.cas", 300)
        self.cas_blobs[cas_fid] = base64.b64encode(
            json.dumps(cas_info, ensure_ascii=False).encode())
        self.ep_fid = self._add(tv, "S01E01.mkv", 2 * 1024 ** 3)
        self._add(tv, "S01E01.ass", 1024)

    # ---- 客户端接口 ----
    def list_files(self, folder_id=None):
        return list(self.tree.get(folder_id or "/", []))

    def personal_create(self, payload):
        self.dl_calls += 1
        if payload.get("type") == "folder":
            fid = self._add(payload["parentFileId"], payload["name"], 0, True)
        else:
            self.restores += 1
            fid = self._add(payload["parentFileId"], payload["name"], payload["size"])
        return {"success": True, "data": {"fileId": fid, "fileName": payload["name"],
                                          "rapidUpload": True, "exist": False}}

    def personal_get_link(self, file_id):
        return (f"http://127.0.0.1:1/cdn/{file_id}"
                "?X-Amz-Date=20260101T000000Z&X-Amz-Expires=900")

    def get_download_url(self, file_id, path=""):
        self.dl_calls += 1
        if file_id in self.cas_blobs:
            return f"http://127.0.0.1:1/raw/{file_id}"
        return self.personal_get_link(file_id)

    def personal_delete(self, ids):
        for d, items in self.tree.items():
            self.tree[d] = [i for i in items if i.file_id not in ids]
        return {"success": True}

    # ---- requests.Session 替身（只用于下载 .cas 与字幕）----
    class _Resp:
        def __init__(self, content):
            self.content = content
            self.status_code = 200

        def raise_for_status(self):
            pass

        def close(self):
            pass

    @property
    def _session(self):
        return self

    def get(self, url, **kw):
        if "/raw/" in url:
            key = url.rsplit("/raw/", 1)[-1]
            return self._Resp(self.cas_blobs.get(key, b""))
        return self._Resp(VIDEO)


MOCK = MockClient()
appmod.get_client = lambda cfg: MOCK
appmod.link_dead = lambda key, url: False          # 本地没有真 CDN，跳过探测

failures = []


def check(name, cond, extra=""):
    print(("  ✔ " if cond else "  ✘ ") + name + (("  " + str(extra)) if extra and not cond else ""))
    if not cond:
        failures.append(name)


def run_task(name):
    t = next(x for x in appmod.STORE.load_tasks() if x["name"] == name)
    return t, appmod.run_strm_job(appmod._build_job(t, appmod.load_config()))


def main():
    client = appmod.app.test_client()

    print("1) 建任务")
    r = client.post("/api/tasks", json={
        "name": "movie", "folder_id": MOCK.movie_dir, "folder_path": "/movies",
        "sync_mode": "incremental", "delete_orphans": True, "cron": "0 3 * * *"})
    check("创建任务", r.status_code == 200, r.get_json())
    check("下次运行时间已计算", bool(r.get_json()["task"]["next_run"]), r.get_json())
    r2 = client.post("/api/tasks", json={"name": "tv", "folder_id": MOCK.tv_dir,
                                         "folder_path": "/tv",
                                         "delete_orphans": True})
    check("创建第二个任务", r2.status_code == 200, r2.get_json())
    bad = client.post("/api/tasks", json={"name": "bad", "cron": "not-a-cron"})
    check("非法 crontab 被拒绝", bad.status_code == 400, bad.get_json())
    tid = r.get_json()["task"]["id"]
    upd = client.put("/api/tasks/" + tid, json={"sync_mode": "force"})
    check("编辑任务切换为强同步",
          upd.status_code == 200 and upd.get_json()["task"]["sync_mode"] == "force",
          upd.get_json())
    upd = client.put("/api/tasks/" + tid, json={"sync_mode": "incremental"})
    check("编辑任务切回增量",
          upd.status_code == 200 and upd.get_json()["task"]["sync_mode"] == "incremental",
          upd.get_json())

    print("2) 运行任务（真实走 strm 生成器）")
    for name in ("movie", "tv"):
        _t, s = run_task(name)
        print("      ", name, json.dumps({k: s.get(k) for k in
                                          ("created", "cas", "copied", "error_count")},
                                         ensure_ascii=False))
        check(f"{name} 无错误", not s.get("error_count"), s.get("errors"))

    out = os.path.join(WORK, "strm")
    movie_dir = os.path.join(out, "movie")     # 任务名 = 壳目录
    tv_dir = os.path.join(out, "tv")
    check("普通视频 strm", os.path.exists(os.path.join(movie_dir, "普通电影.mkv.strm")))
    check("字幕已下载", os.path.exists(os.path.join(movie_dir, "普通电影.chs.srt")))
    cas_path = os.path.join(movie_dir, "秒传电影.mkv.strm")
    check("CAS strm 去掉了 .cas 尾巴", os.path.exists(cas_path))
    cas_text = open(cas_path, encoding="utf-8").read() if os.path.exists(cas_path) else ""
    check("CAS strm 带 ?cas= 参数", "?cas=" in cas_text and "秒传电影.mkv.cas" in cas_text, cas_text)
    check("strm 内容是播放器可访问的基础地址", cas_text.startswith("http://player.local:8025/d/"),
          cas_text)

    print("3) 302 播放（普通文件）")
    fid = open(os.path.join(movie_dir, "普通电影.mkv.strm"), encoding="utf-8").read().rsplit("/", 1)[-1]
    r = client.get(f"/d/{fid}")
    check("普通文件返回 302", r.status_code == 302, r.status_code)
    check("302 目标是云盘直链", "X-Amz-Expires" in (r.headers.get("Location") or ""),
          r.headers.get("Location"))
    check("禁缓存头", "no-store" in (r.headers.get("Cache-Control") or ""))

    print("4) 302 播放（.cas 秒传还原）")
    cas_fid = cas_text.split("/d/")[-1].split("?")[0]
    r = client.get(f"/d/{cas_fid}?cas=秒传电影.mkv.cas")
    check("CAS 返回 302", r.status_code == 302, r.get_data(as_text=True)[:200])
    check("CAS 触发秒传还原", MOCK.restores == 1, f"restores={MOCK.restores}")
    r = client.get(f"/d/{cas_fid}?cas=秒传电影.mkv.cas")
    check("重复播放不重复秒传", MOCK.restores == 1, f"restores={MOCK.restores}")
    restorer = appmod.get_restorer(appmod.load_config(), MOCK)
    parts = cas_mod.build_part_infos(120 * 1024 ** 3)
    check("120GB 使用 512MB 分片", parts[0]["partSize"] == 512 * 1024 ** 2)
    check("分片覆盖全文件", sum(p["partSize"] for p in parts) == 120 * 1024 ** 3)
    check("会话登记", restorer.session_count() == 1)
    # 关闭秒传：同一个链接应当按普通文件处理，不再还原
    appmod.STORE.update_config({"cas_enabled": False})
    client.get(f"/d/{cas_fid}?cas=秒传电影.mkv.cas")
    appmod.STORE.update_config({"cas_enabled": True})
    check("关闭秒传后不再还原", MOCK.restores == 1, f"restores={MOCK.restores}")

    print("5) 代理模式")
    appmod.STORE.update_config({"play_proxy": True})
    r = client.get(f"/d/{fid}?proxy=1")
    check("代理模式下上游不可达时给出可读错误", r.status_code >= 400, r.status_code)
    appmod.STORE.update_config({"play_proxy": False})
    # 代理失败时清掉过直链缓存，这里手动恢复「已取到直链」的状态
    appmod._link_cache.clear()

    print("6) 诊断 / 状态接口")
    d = client.get("/api/diag").get_json()
    check("诊断有记录", d["ok"] and len(d["items"]) >= 2, len(d.get("items") or []))
    st = client.get("/api/strm/status").get_json()
    check("状态接口可用", "running" in st and st["running"] is False, st)
    casst = client.get("/api/cas/status").get_json()
    check("秒传状态显示 500GB", casst["single_file_limit"] == "500GB", casst)
    check("临时目录已建立", bool(restorer.get_temp_dir()))
    check("存储挂载检测", client.get("/api/storage").get_json()["ok"])
    cfgjson = client.get("/api/config").get_json()
    check("配置接口不回显明文凭据", not cfgjson.get("authorization"), cfgjson.get("authorization"))

    print("7) 增量 / 删除孤儿")
    _t, s = run_task("movie")
    print("     [run2]", json.dumps({k: s.get(k) for k in
                                    ("created", "updated", "skipped", "errors", "logs")},
                                   ensure_ascii=False))
    check("增量模式不重复生成 strm", s["created"] == 0 and s["updated"] == 0, s)
    MOCK.personal_delete([MOCK.ep_fid])          # 云端删掉这一集
    _t, s = run_task("tv")
    check("孤儿 strm 被删除", s["deleted"] >= 1 and
          not os.path.exists(os.path.join(tv_dir, "S01E01.mkv.strm")), s)
    check("字幕文件不受影响", os.path.exists(os.path.join(tv_dir, "S01E01.ass")))
    check(".cas 同名的壳文件不被当字幕下载",
          not os.path.exists(os.path.join(movie_dir, "秒传电影.mkv")))

    print("8) 任务删除（同时清理 strm）")
    mid = next(t["id"] for t in appmod.STORE.load_tasks() if t["name"] == "movie")
    r = client.delete(f"/api/tasks/{mid}?clean_output=1")
    check("删除任务并清理", r.status_code == 200 and r.get_json()["cleaned"] >= 2, r.get_json())
    check("movie 目录已清空",
          not os.path.exists(os.path.join(movie_dir, "普通电影.mkv.strm")))

    print()
    if failures:
        print(f"❌ 失败 {len(failures)} 项: {failures}")
        return 1
    print("✅ 端到端全部通过")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(code)
