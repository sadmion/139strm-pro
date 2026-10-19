# -*- coding: utf-8 -*-
"""离线单元测试：签名、加解密、CAS 分片/解析、STRM 生成、cron、配置存储、路由。

全部不访问网络：网络调用一律用假 client 替身。
运行：python -m pytest -q
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from yun139 import cas as cas_mod
from yun139 import crypto, cron
from yun139.client import FileItem, Yun139Client, extract_fast_login_cookies
from yun139.errors import QuotaError, Yun139Error
from yun139.store import DEFAULT_CONFIG, Store, normalize_task
from yun139.strm import StrmGenerator, sanitize_name


# ---------------------------------------------------------------------------
# crypto
# ---------------------------------------------------------------------------

def go_reference_sign(body: str, ts: str, rand: str) -> str:
    """独立实现一遍 Go 的 calSign，用来交叉验证。"""
    from urllib.parse import quote
    r = quote(body, safe="-_.~").replace("+", "%20")
    for a, b in (("%21", "!"), ("%27", "'"), ("%28", "("), ("%29", ")"), ("%2A", "*")):
        r = r.replace(a, b)
    ordered = "".join(sorted(r, key=lambda ch: ch.encode("utf-8")))
    b64 = base64.b64encode(ordered.encode()).decode()
    md5 = lambda s: hashlib.md5(s.encode()).hexdigest()
    return md5(md5(b64) + md5(ts + ":" + rand)).upper()


@pytest.mark.parametrize("body", [
    '{"a":1}',
    '{"name":"电影.mp4","size":4554461676}',
    'a b!c*d(e)\'f~g/中文"',
    '{"x":"&<>","y":[1,2]}',
])
def test_cal_sign_matches_go(body):
    ts, rand = "2026-01-01 00:00:00", "abcd1234"
    assert crypto.cal_sign(body, ts, rand) == go_reference_sign(body, ts, rand)


def test_encode_uri_component_matches_go_queryscape():
    # Go: url.QueryEscape 不转义 -_.~ ，空格转 + 后手工还原为 %20
    assert crypto.encode_uri_component("a b/c!*d(e)'~") == "a%20b%2Fc!*d(e)'~"
    assert crypto.encode_uri_component("电影.mp4") == "%E7%94%B5%E5%BD%B1.mp4"


def test_go_float_str():
    assert crypto.go_float_str(13800138000) == "1.3800138e+10"
    assert crypto.go_float_str(0.0001) == "0.0001"
    assert crypto.go_float_str(0.00001) == "1e-05"
    assert crypto.go_float_str(1234567) == "1.234567e+06"
    assert crypto.go_float_str(123456) == "123456"
    assert crypto.go_float_str(0) == "0"


def test_sorted_json_stringify_sorts_and_escapes():
    out = crypto.sorted_json_stringify({"z": 1, "a": "x<y&z>", "m": [True, None]})
    assert out == '{"a":"x\\u003cy\\u0026z\\u003e","m":[true,null],"z":1}'


def test_encrypt_decrypt_roundtrip():
    body = {"msisdn": "13800138000", "extInfo": {"ifOpenAccount": "0"}}
    blob = crypto.encrypt_body(body)
    raw = base64.b64decode(blob)
    iv, ct = raw[:16], raw[16:]
    plain = crypto.aes_cbc_decrypt(ct, bytes.fromhex(crypto.KEY_HEX_1), iv).decode()
    # Go 语义：纯数字字符串会被当成数字，输出 1.3800138e+10
    assert plain == '{"extInfo":{"ifOpenAccount":0},"msisdn":1.3800138e+10}'
    # 第一层 RSA/AES 的响应解密路径
    enc = base64.b64encode(iv + crypto.aes_cbc_encrypt(b"hello", bytes.fromhex(crypto.KEY_HEX_1), iv))
    assert crypto.decrypt_response(enc.decode()) == b"hello"


def test_pkcs7_rejects_bad_padding():
    with pytest.raises(ValueError):
        crypto.pkcs7_unpad(b"\x00" * 8)


# ---------------------------------------------------------------------------
# CAS
# ---------------------------------------------------------------------------

def make_cas_bytes(name="电影.mkv", size=4554461676, sha256=None):
    payload = {
        "provider": "139",
        "name": name,
        "size": size,
        "md5": "a" * 32,
        "sha256": sha256 or "b" * 64,
        "sliceMd5": "c" * 32,
    }
    return base64.b64encode(json.dumps(payload, ensure_ascii=False).encode())


def test_cas_decode_ok():
    info = cas_mod.decode(make_cas_bytes())
    assert info.name == "电影.mkv"
    assert info.size == 4554461676
    assert info.sha256 == "b" * 64


@pytest.mark.parametrize("raw", [b"", b"!!!notbase64", base64.b64encode(b"not json"),
                                 base64.b64encode(b'{"name":"x"}')])
def test_cas_decode_rejects_garbage(raw):
    with pytest.raises(cas_mod.CASError):
        cas_mod.decode(raw)


def test_restore_name_derivation():
    info = cas_mod.decode(make_cas_bytes())
    assert cas_mod.resolve_restore_name("电影.mkv.cas", info) == "电影.mkv"
    assert cas_mod.resolve_restore_name("电影.cas", info) == "电影.mkv"
    assert cas_mod.resolve_restore_name("Movie.2024.mkv.cas", info) == "Movie.2024.mkv"
    with pytest.raises(cas_mod.CASError):
        cas_mod.resolve_restore_name("x.txt", info)


def test_part_infos_sizes():
    # 40GB -> 512MB 分片
    size = 40 * 1024 ** 3
    parts = cas_mod.build_part_infos(size)
    ps = 512 * 1024 ** 2
    assert len(parts) == (size + ps - 1) // ps
    assert parts[0]["partSize"] == ps
    assert parts[0]["parallelHashCtx"]["partOffset"] == 0
    assert sum(p["partSize"] for p in parts) == size
    assert parts[-1]["partNumber"] == len(parts)
    # 4GB -> 100MB 分片
    small = cas_mod.build_part_infos(4 * 1024 ** 3)
    assert small[0]["partSize"] == 100 * 1024 ** 2


def test_part_size_threshold():
    assert cas_mod.part_size_for(30 * 1024 ** 3) == 100 * 1024 ** 2
    assert cas_mod.part_size_for(31 * 1024 ** 3) == 512 * 1024 ** 2


def test_temp_base_name():
    assert cas_mod.temp_base_name("TEMP_1756000000000_01357_电影 (1).mkv") == "电影.mkv"
    assert cas_mod.temp_base_name("电影.mkv") == "电影.mkv"


# ---------------------------------------------------------------------------
# Fake client
# ---------------------------------------------------------------------------

class FakeClient:
    """最小可用的云盘替身：一个内存目录树 + 秒传 'restore' 行为。"""

    cloud_type = "personal_new"
    root_folder_id = "/"
    timeout = (1, 1)

    def __init__(self, files=None):
        self.dirs = {"/": []}          # dir_id -> [FileItem]
        self.links = {}
        self.created = []
        self.deleted = []
        self._seq = 0
        self.quota = False
        self.session = self

    # list_files
    def list_files(self, folder_id=None):
        return list(self.dirs.get(folder_id or "/", []))

    def mkdir(self, parent, name):
        self._seq += 1
        fid = f"dir{self._seq}"
        self.dirs[fid] = []
        self.dirs.setdefault(parent, []).append(FileItem(fid, name, is_folder=True))
        return fid

    def add_file(self, parent, name, size=1, fid=None):
        self._seq += 1
        fid = fid or f"f{self._seq}"
        self.dirs.setdefault(parent, []).append(FileItem(fid, name, size=size))
        return fid

    def personal_create(self, payload):
        if self.quota:
            raise QuotaError("权益不足")
        self.created.append(payload)
        parent = payload["parentFileId"]
        if payload.get("type") == "folder":
            fid = self.mkdir(parent, payload["name"])
        else:
            fid = self.add_file(parent, payload["name"], payload["size"])
        return {"success": True, "data": {"fileId": fid, "fileName": payload["name"],
                                          "rapidUpload": True, "exist": False}}

    def personal_get_link(self, file_id):
        url = f"https://cdn.example/{file_id}?X-Amz-Date=20260101T000000Z&X-Amz-Expires=900"
        self.links[file_id] = url
        return url

    def get_download_url(self, file_id, path=""):
        if file_id in self.raw:
            return f"https://cdn.example/raw/{file_id}"
        return self.personal_get_link(file_id)

    def personal_delete(self, ids):
        self.deleted.extend(ids)
        for dir_id, items in self.dirs.items():
            self.dirs[dir_id] = [i for i in items if i.file_id not in ids]
        return {"success": True}

    # requests.Session 替身
    raw = {}

    class _Resp:
        status_code = 200

        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            pass

    @property
    def _session(self):
        return self

    def get(self, url, **kw):
        key = url.rsplit("/", 1)[-1]
        # 下载地址形如 https://cdn.example/raw/<file_id>
        if "/raw/" in url:
            key = url.rsplit("/raw/", 1)[-1]
        return self._Resp(self.raw.get(key, b""))


def test_cas_restore_uses_sha256_and_full_parts(tmp_path):
    client = FakeClient()
    cas_bytes = make_cas_bytes(size=4 * 1024 ** 3)
    fid = client.add_file("/", "电影.mkv.cas", size=300)
    client.raw[fid] = cas_bytes
    restorer = cas_mod.CASRestorer(client, cache_file=str(tmp_path / "c.json"))
    url, size, temp_id, real_name, restored = restorer.fetch_link_result(fid, "电影.mkv.cas")
    assert url.startswith("https://cdn.example/")
    assert size == 4 * 1024 ** 3
    assert restored is True
    assert real_name.endswith("电影.mkv")           # 云端实际文件名带 TEMP_ 前缀
    assert cas_mod.temp_base_name(real_name) == "电影.mkv"
    req = [c for c in client.created if c.get("type") == "file"][0]
    assert req["contentHash"] == "b" * 64
    assert req["contentHashAlgorithm"] == "SHA256"
    assert req["partInfos"][0]["partSize"] == 100 * 1024 ** 2
    # 临时目录被创建出来了
    assert restorer.get_temp_dir()


def test_cas_reuses_single_temp_file(tmp_path):
    client = FakeClient()
    cas_bytes = make_cas_bytes()
    fid = client.add_file("/", "电影.mkv.cas", size=300)
    client.raw[fid] = cas_bytes
    restorer = cas_mod.CASRestorer(client, cache_file=str(tmp_path / "c.json"))
    r1 = restorer.fetch_link_result(fid, "电影.mkv.cas")
    r2 = restorer.fetch_link_result(fid, "电影.mkv.cas")
    r3 = restorer.fetch_link_result(fid, "电影.mkv.cas")
    # 只秒传过一次，没有反复还原
    assert len([c for c in client.created if c.get("type") == "file"]) == 1
    assert r1[2] == r2[2] == r3[2]           # 同一个临时文件
    assert r2[4] is False and r3[4] is False  # 后两次是「复用」不是「重新还原」
    assert restorer.session_count() == 1


def test_cas_quota_error_is_friendly(tmp_path):
    client = FakeClient()
    cas_bytes = make_cas_bytes()
    fid = client.add_file("/", "电影.mkv.cas", size=300)
    client.raw[fid] = cas_bytes
    restorer = cas_mod.CASRestorer(client, cache_file=str(tmp_path / "c.json"))
    restorer.ensure_temp_dir()          # 先把临时目录建好
    client.quota = True                 # 之后秒传被云盘以「权益不足」拒绝
    with pytest.raises(cas_mod.CASError) as ei:
        restorer.fetch_link_result(fid, "电影.mkv.cas")
    assert "权益不足" in str(ei.value)


def test_cas_skips_non_video_by_default(tmp_path):
    client = FakeClient()
    fid = client.add_file("/", "doc.pdf.cas", size=10)
    client.raw[fid] = make_cas_bytes(name="doc.pdf")
    restorer = cas_mod.CASRestorer(client, cache_file=str(tmp_path / "c.json"))
    with pytest.raises(cas_mod.CASError):
        restorer.fetch_link_result(fid, "doc.pdf.cas")
    restorer.allow_all_ext = True
    restorer.fetch_link_result(fid, "doc.pdf.cas")


# ---------------------------------------------------------------------------
# STRM
# ---------------------------------------------------------------------------

def test_sanitize_name():
    assert sanitize_name('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"
    assert sanitize_name("  电影. ") == "电影"
    assert sanitize_name("") == "_"


def build_tree(client):
    root = "/"
    d = client.mkdir(root, "电影")
    client.add_file(d, "A.mkv", size=10)
    client.add_file(d, "B.mp4.cas", size=300)
    client.add_file(d, "C.srt", size=10)
    sub = client.mkdir(d, "花絮")
    client.add_file(sub, "D.mkv", size=10)
    client.add_file(root, "readme.txt", size=1)


def test_generate_strm_tree(tmp_path):
    client = FakeClient()
    build_tree(client)
    gen = StrmGenerator(client, "http://host:8025", str(tmp_path), min_size_mb=0)
    gen.generate("/", "movie")
    out = tmp_path / "movie"
    assert (out / "电影" / "A.mkv.strm").read_text() == "http://host:8025/d/f2"
    cas_txt = (out / "电影" / "B.mp4.strm").read_text()
    assert cas_txt.startswith("http://host:8025/d/f3?cas=B.mp4.cas")
    assert "B.mp4.cas" in cas_txt
    assert (out / "电影" / "花絮" / "D.mkv.strm").exists()
    # 字幕被下载到本地（FakeClient 的 get 返回空内容）
    assert (out / "电影" / "C.srt").exists()
    s = gen.summary()
    assert s["created"] >= 3 and s["cas"] == 1


def test_incremental_vs_force(tmp_path):
    client = FakeClient()
    build_tree(client)
    gen = StrmGenerator(client, "http://host:8025", str(tmp_path))
    gen.generate("/", "movie")
    gen2 = StrmGenerator(client, "http://host:8025", str(tmp_path))
    gen2.generate("/", "movie")
    assert gen2.created == 0 and gen2.updated == 0
    # 基础地址变了 -> 增量模式也会把内容不一致的 strm 更新掉
    gen3 = StrmGenerator(client, "http://new:8025", str(tmp_path))
    gen3.generate("/", "movie")
    assert gen3.updated >= 3
    assert "http://new:8025" in (tmp_path / "movie" / "电影" / "A.mkv.strm").read_text()


def test_delete_orphans_only_removes_strm(tmp_path):
    client = FakeClient()
    build_tree(client)
    gen = StrmGenerator(client, "http://host:8025", str(tmp_path))
    gen.generate("/", "movie")
    orphan_dir = tmp_path / "movie" / "电影" / "花絮"
    (orphan_dir / "Gone.mkv.strm").write_text("http://old")
    (orphan_dir / "keep.srt").write_text("sub")
    # 云端删掉 D，再同步
    client.dirs[list(client.dirs)[-1]] = []
    gen2 = StrmGenerator(client, "http://host:8025", str(tmp_path),
                         delete_orphans=True)
    gen2.generate("/", "movie")
    assert not (orphan_dir / "D.mkv.strm").exists()
    assert not (orphan_dir / "Gone.mkv.strm").exists()
    assert (orphan_dir / "keep.srt").exists()          # 不删用户文件
    assert gen2.deleted >= 2


def test_min_size_threshold(tmp_path):
    client = FakeClient()
    client.add_file("/", "small.mp4", size=1024)
    client.add_file("/", "big.mp4", size=10 * 1024 * 1024)
    gen = StrmGenerator(client, "http://h", str(tmp_path), min_size_mb=5)
    gen.generate("/", "")
    assert not (tmp_path / "small.mp4.strm").exists()
    assert (tmp_path / "big.mp4.strm").exists()


def test_cancel_aborts(tmp_path):
    client = FakeClient()
    build_tree(client)
    gen = StrmGenerator(client, "http://h", str(tmp_path))
    gen.cancel()
    with pytest.raises(Exception):
        gen.generate("/", "x")


def test_strm_requires_base_url(tmp_path):
    gen = StrmGenerator(FakeClient(), "", str(tmp_path))
    with pytest.raises(ValueError):
        gen.generate("/", "x")


# ---------------------------------------------------------------------------
# cron
# ---------------------------------------------------------------------------

def test_cron_validation():
    assert cron.is_valid_cron("0 3 * * *")
    assert cron.is_valid_cron("*/30 * * * *")
    assert cron.is_valid_cron("0 3 * * 1")
    assert not cron.is_valid_cron("0 3 * *")
    assert not cron.is_valid_cron("61 * * * *")
    assert not cron.is_valid_cron("a b c d e")


def test_cron_next_run():
    now = datetime(2026, 10, 19, 21, 0)      # 周一
    nxt = cron.next_run_from_cron("0 3 * * *", now)
    assert nxt == datetime(2026, 10, 20, 3, 0)
    nxt = cron.next_run_from_cron("*/30 * * * *", now)
    assert nxt == datetime(2026, 10, 19, 21, 30)
    nxt = cron.next_run_from_cron("0 0 * * 0", now)   # 周日
    assert nxt.weekday() == 6


def test_cron_matches_sunday_both_forms():
    sunday = datetime(2026, 10, 25, 3, 0)
    assert cron.cron_matches("0 3 * * 0", sunday)
    assert cron.cron_matches("0 3 * * 7", sunday)


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

def test_store_roundtrip_and_backup(tmp_path):
    st = Store(str(tmp_path / "config.json"))
    cfg = st.load_config()
    assert cfg["output_dir"] == DEFAULT_CONFIG["output_dir"]
    cfg["authorization"] = "abc"
    st.save_config(cfg)
    assert st.load_config()["authorization"] == "abc"
    # 第二次保存时，第一次的内容被留成 .bak
    st.save_config({**cfg, "authorization": "def"})
    assert st.load_config()["authorization"] == "def"
    bak = json.loads((tmp_path / "config.json.bak").read_text(encoding="utf-8"))
    assert bak["authorization"] == "abc"


def test_store_recovers_from_corrupt_file(tmp_path):
    path = tmp_path / "config.json"
    st = Store(str(path))
    cfg = st.load_config()
    cfg["authorization"] = "good-token"
    st.save_config(cfg)
    st.save_config({**cfg, "authorization": "good-token2"})   # 产生 .bak
    path.write_text("{ broken", encoding="utf-8")             # 模拟写坏
    st2 = Store(str(path))
    out = st2.load_config()
    assert out["authorization"] == "good-token"
    assert "损坏" in st2.last_error


def test_store_sanitize_types(tmp_path):
    st = Store(str(tmp_path / "config.json"))
    cfg = st.load_config()
    cfg["cas_temp_ttl"] = "abc"
    cfg["media_ext"] = "MKV, .mp4 ,"
    cfg["min_size_mb"] = "x"
    out = st.save_config(cfg)
    assert out["cas_temp_ttl"] == 300
    assert out["media_ext"] == ["mkv", "mp4"]
    assert out["min_size_mb"] == 0


def test_normalize_task():
    t = normalize_task({"name": "movie", "sync_mode": "force"})
    assert t["delete_orphans"] is True          # 强同步默认删除孤儿
    t2 = normalize_task({"name": "tv", "sync_mode": "incremental"})
    assert t2["delete_orphans"] is False
    assert t2["id"] and t2["enabled"] is True


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------

def test_cookie_extract():
    sid, rmkey = extract_fast_login_cookies("A=1; Os_SSo_Sid=xyz; RMKEY=kk")
    assert (sid, rmkey) == ("xyz", "kk")


def test_authorization_split_rules():
    c = Yun139Client(authorization="Basic abc")
    with pytest.raises(Yun139Error):
        c._split_authorization()
    good = base64.b64encode(b"pc:13800138000:tok|a|b|9999999999999").decode()
    c2 = Yun139Client(authorization=good)
    prefix, acc, tok = c2._split_authorization()
    assert (prefix, acc) == ("pc", "13800138000")
    assert c2.get_expire_time() is not None


def test_pc_client_info_declares_500gb_type():
    pc = Yun139Client(authorization="x", client_type="pc")
    web = Yun139Client(authorization="x", client_type="web")
    assert pc._client_info().split("|")[2] == "13"
    assert web._client_info().split("|")[2] == "9"


# ---------------------------------------------------------------------------
# 路由（不触网）
# ---------------------------------------------------------------------------

def test_routes_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.json"))
    import app as appmod
    appmod.STORE = Store(str(tmp_path / "config.json"))
    client = appmod.app.test_client()
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 200
    r = client.get("/api/config")
    assert r.status_code == 200 and "output_dir" in r.get_json()
    r = client.post("/api/tasks", json={"name": "movie", "folder_id": "/",
                                        "folder_path": "/movie",
                                        "cron": "0 3 * * *"})
    assert r.status_code == 200, r.get_json()
    tid = r.get_json()["task"]["id"]
    assert client.get("/api/tasks").get_json()["tasks"][0]["next_run"]
    bad = client.post("/api/tasks", json={"name": "x", "cron": "bad"})
    assert bad.status_code == 400
    assert client.put("/api/tasks/" + tid, json={"name": "movie2"}).status_code == 200
    assert client.delete("/api/tasks/" + tid).status_code == 200
    # 未配置凭据时，播放端点给出 503 + 中文提示
    r = client.get("/d/abc")
    assert r.status_code == 503
    assert "Authorization" in r.get_data(as_text=True)


def test_direct_link_302(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.json"))
    import app as appmod
    appmod.STORE = Store(str(tmp_path / "config.json"))
    st = appmod.STORE
    st.update_config({"authorization": "tok"})

    class C:
        def get_download_url(self, fid, path=""):
            return "https://cdn.example/x?X-Amz-Date=20260101T000000Z&X-Amz-Expires=900"

    monkeypatch.setattr(appmod, "get_client", lambda cfg: C())
    monkeypatch.setattr(appmod, "link_dead", lambda key, url: False)
    appmod._link_cache.clear()
    client = appmod.app.test_client()
    r = client.get("/d/abc")
    assert r.status_code == 302
    assert r.headers["Location"].startswith("https://cdn.example/")
    assert "no-store" in r.headers["Cache-Control"]
