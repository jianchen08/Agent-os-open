# @feature: FP-0.2.PACK 打包/部署 CI 门（种子打包期烘焙面） | @ci: python-test
"""materialize_seed.py 发现缓存烘焙单测（T4/R-2 层 2，2026-11-01）。

覆盖：缓存内容与 survey 一致性（逐文件 sha256/候选表/自指排除/符号链接面）、
幂等跳过（二次运行零重写且产物字节不变）、陈旧缓存原位换血（防重名残留）、
旗标关闭不发射、面内硬链接拒烘 fail-closed。
"""
import hashlib
import io
import json
import sys
import tarfile
import zlib  # noqa: F401  (占位防误删；实际压缩用 zstandard)

import pytest
import zstandard as zstd

REPO_ROOT = __import__("os").path.dirname(__file__) + "/../.."
sys.path.insert(0, __import__("os").path.join(REPO_ROOT, "deploy"))

import materialize_seed as ms  # noqa: E402

CACHE_ENTRY = "opt/agentos/plugins/shared/discovery-cache.json"

# 迷你种子插件面：两个插件（嵌套文件深一层）、一个符号链接、面内非插件目录
# （sdk/_libs——缓存须覆盖但不算候选）、一个面外文件（etc/resolv.conf 待写定）。
FACE_FILES = {
    "opt/agentos/plugins/shared/llm/plugin.json": b'{"id": "llm_service", "version": "0.1.0"}',
    "opt/agentos/plugins/shared/llm/server.py": b"print('llm server')\n",
    "opt/agentos/plugins/shared/llm_core/plugin.json": b'{"id": "pipeline_llm_core", "version": "0.2.0"}',
    "opt/agentos/plugins/shared/llm_core/sub/deep.py": b"DEEP = 42\n",
    "opt/agentos/plugins/sdk/pyproject.toml": b"[project]\nname = 'sdk'\n",
    "opt/agentos/plugins/shared/_libs/hostlib.py": b"LIB = 1\n",
}
FACE_LINKS = {"opt/agentos/plugins/shared/llm/link.py": "server.py"}
RESOLV_OLD = b"nameserver 8.8.8.8\n"


def _add(tar, name, data=None, linkname=None, ftype=tarfile.REGTYPE):
    info = tarfile.TarInfo(name)
    info.type = ftype
    info.mode = 0o644
    info.mtime = 0
    if ftype == tarfile.REGTYPE:
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    elif ftype in (tarfile.SYMTYPE, tarfile.LNKTYPE):
        info.linkname = linkname
        tar.addfile(info)
    else:
        raise AssertionError("fixture 只覆盖常规文件/符号链接/硬链接")


def _compress(entries, dest):
    """entries: (name, data|None, linkname|None, ftype) 列表 → zstd 压缩 tar。"""
    raw = dest + ".raw.tar"
    with tarfile.open(raw, "w|", format=tarfile.GNU_FORMAT) as tar:
        for name, data, linkname, ftype in entries:
            _add(tar, name, data, linkname, ftype)
    with open(raw, "rb") as src, open(dest, "wb") as out:
        zstd.ZstdCompressor(level=3).copy_stream(src, out)
    import os
    os.unlink(raw)


def base_entries():
    es = [(n, d, None, tarfile.REGTYPE) for n, d in FACE_FILES.items()]
    es.append((RESOLV_ENTRY := "etc/resolv.conf", RESOLV_OLD, None, tarfile.REGTYPE))
    for n, t in FACE_LINKS.items():
        es.append((n, None, t, tarfile.SYMTYPE))
    return es


def run_ms(tmp_path, seed, extra):
    """跑 materialize_seed.main，返回 (返回码, stdout, stderr)。"""
    out = str(tmp_path / "out.tar.zst")
    argv = ["materialize_seed.py", "--seed", str(seed), "--out", out] + extra
    old = sys.argv
    sys.argv = argv
    try:
        rc = ms.main()
    finally:
        sys.argv = old
    return rc, out


def read_entry(path_zst, name):
    """解压种子，返回 (全部条目名, 指定条目内容 bytes|None)。"""
    raw = path_zst + ".probe.tar"
    with open(path_zst, "rb") as fh, open(raw, "wb") as out:
        zstd.ZstdDecompressor().copy_stream(fh, out)
    names = []
    content = None
    with tarfile.open(raw, "r|") as tf:
        while True:
            m = tf.next()
            if m is None:
                break
            names.append(m.name)
            if m.name == name and m.isfile():
                content = tf.extractfile(m).read()
    import os
    os.unlink(raw)
    return names, content


def test_bake_emits_cache_consistent_with_survey(tmp_path):
    seed = str(tmp_path / "seed.tar.zst")
    _compress(base_entries(), seed)
    rc, out = run_ms(tmp_path, seed, ["--bake-discovery-cache"])
    assert rc == 0
    names, cache_raw = read_entry(out, CACHE_ENTRY)
    assert cache_raw is not None, "缓存条目必须发射"
    assert names.count(CACHE_ENTRY) == 1
    cache = json.loads(cache_raw.decode("utf-8"))
    assert cache["version"] == 1
    assert cache["face_root"] == "/opt/agentos/plugins"
    # 面内常规文件逐个对上（相对 face_root 的键）+ 内容摘要与 fixture 一致
    expect_files = {
        n[len("opt/agentos/plugins/"):]: hashlib.sha256(d).hexdigest()
        for n, d in FACE_FILES.items()
    }
    assert cache["files"] == expect_files
    # 符号链接面（目标串保真）
    assert cache["links"] == {
        n[len("opt/agentos/plugins/"):]: t for n, t in FACE_LINKS.items()
    }
    # 候选表：manifest 目录按扫描根归组（与内核 discover_plugin_roots 同语义）
    assert cache["plugin_dirs"] == {
        "/opt/agentos/plugins/shared": {
            "llm": "plugin.json",
            "llm_core": "plugin.json",
        }
    }
    # 自指排除：缓存文件本体不入面摘要
    assert "shared/discovery-cache.json" not in cache["files"]


def test_bake_idempotent_second_run_zero_rewrite(tmp_path):
    seed = str(tmp_path / "seed.tar.zst")
    _compress(base_entries(), seed)
    rc1, out = run_ms(tmp_path, seed, ["--bake-discovery-cache"])
    assert rc1 == 0
    import hashlib as _h
    import os
    first = _h.sha256(open(out, "rb").read()).hexdigest()
    # 二次运行：全条件齐备（含缓存现物全等）→ 零重写
    argv = ["materialize_seed.py", "--seed", out, "--out",
            str(tmp_path / "again.tar.zst"), "--bake-discovery-cache"]
    old = sys.argv
    sys.argv = argv
    try:
        rc2 = ms.main()
    finally:
        sys.argv = old
    assert rc2 == 0
    # 零重写路径直接 return 0 不产出 again.tar.zst
    assert not os.path.exists(str(tmp_path / "again.tar.zst"))
    second_names, second_cache = read_entry(out, CACHE_ENTRY)
    assert second_cache is not None and json.loads(second_cache)["version"] == 1


def test_stale_cache_replaced_in_place_no_duplicate(tmp_path):
    seed = str(tmp_path / "seed.tar.zst")
    entries = base_entries()
    stale = json.dumps({"version": 1, "files": {"bogus": "0" * 64}}).encode()
    entries.append((CACHE_ENTRY, stale, None, tarfile.REGTYPE))
    _compress(entries, seed)
    rc, out = run_ms(tmp_path, seed, ["--bake-discovery-cache"])
    assert rc == 0
    names, cache_raw = read_entry(out, CACHE_ENTRY)
    assert cache_raw is not None and b"bogus" not in cache_raw, "陈旧缓存必须换血"
    assert names.count(CACHE_ENTRY) == 1, "原位换血，不得重名残留"


def test_flag_off_emits_no_cache(tmp_path):
    seed = str(tmp_path / "seed.tar.zst")
    _compress(base_entries(), seed)
    rc, out = run_ms(tmp_path, seed, [])
    assert rc == 0
    names, _ = read_entry(out, CACHE_ENTRY)
    assert CACHE_ENTRY not in names, "旗标关闭 = 特性关闭，零发射"


def test_face_hardlink_bakes_via_target_resolution_and_state_byte_equal(tmp_path):
    """硬链接条目摘要经 index 解析目标真实内容（2026-11-01 全量链接线缺口修复）：
    pack dedup 产物（sdk 面 64 条硬链接）走通烘焙；硬链接态与物化态产出缓存
    字节恒一致（幂等语义锁定）。"""
    import hashlib
    import json
    import os

    seed = str(tmp_path / "seed.tar.zst")
    entries = base_entries()
    target_name = "opt/agentos/plugins/shared/llm/server.py"
    link_name = "opt/agentos/plugins/shared/llm/hard.py"
    entries.append((link_name, None, target_name, tarfile.LNKTYPE))
    _compress(entries, seed)

    # 硬链接态烘焙（同一遍完成物化）
    rc, out = run_ms(tmp_path, seed, ["--bake-discovery-cache"])
    assert rc == 0, "可解析硬链接不得拒烘（摘要=目标内容，非零字节失真）"
    _, cache_raw = read_entry(out, CACHE_ENTRY)
    assert cache_raw is not None
    cache = json.loads(cache_raw.decode("utf-8"))
    expect = hashlib.sha256(FACE_FILES[target_name]).hexdigest()
    assert cache["files"]["shared/llm/hard.py"] == expect, "硬链接摘要=目标内容"
    assert cache["files"]["shared/llm/server.py"] == expect

    # 物化态烘焙（从原始硬链接种子先物化不带缓存，再对物化产物烘焙）
    # → 与硬链接态字节全等
    mat_out = str(tmp_path / "mat.tar.zst")
    argv = ["materialize_seed.py", "--seed", seed, "--out", mat_out]
    old = sys.argv
    sys.argv = argv
    try:
        assert ms.main() == 0
    finally:
        sys.argv = old
    assert os.path.exists(mat_out), "物化态产物必须落盘（源含硬链接/待写定条目）"
    bake2_out = str(tmp_path / "bake2.tar.zst")
    argv = ["materialize_seed.py", "--seed", mat_out, "--out", bake2_out,
            "--bake-discovery-cache"]
    sys.argv = argv
    try:
        assert ms.main() == 0
    finally:
        sys.argv = old
    _, cache_raw2 = read_entry(bake2_out, CACHE_ENTRY)
    assert cache_raw2 == cache_raw, "硬链接态与物化态烘焙字节恒一致（幂等语义）"


def test_face_hardlink_unresolvable_target_refuses_fail_closed(tmp_path):
    seed = str(tmp_path / "seed.tar.zst")
    entries = base_entries()
    entries.append((
        "opt/agentos/plugins/shared/llm/hard.py",
        None,
        "opt/agentos/plugins/shared/llm/ghost_target.py",
        tarfile.LNKTYPE,
    ))
    _compress(entries, seed)
    rc, out = run_ms(tmp_path, seed, ["--bake-discovery-cache"])
    assert rc == 3, "index 解析不到的目标 = 真异常，fail-closed 保留"
    assert not __import__("os").path.exists(out)


def test_pycache_artifacts_excluded_from_face_and_idempotent(tmp_path):
    """可派生 Python 工件两侧同律排除（T4 设备轮 bug 对应烘焙侧）：
    __pycache__ 段与 *.pyc 不入面摘要（真源文件照常），幂等不随 pyc 破功。"""
    import os

    seed = str(tmp_path / "seed.tar.zst")
    entries = base_entries()
    entries += [
        ("opt/agentos/plugins/shared/llm/__pycache__/server.cpython-312.pyc",
         b"\x00PYC", None, tarfile.REGTYPE),
        ("opt/agentos/plugins/shared/llm/loose.pyc", b"\x00PYC2", None, tarfile.REGTYPE),
        ("opt/agentos/plugins/shared/llm_core/sub/__pycache__/deep.cpython-312.pyc",
         b"\x00PYC3", None, tarfile.REGTYPE),
    ]
    _compress(entries, seed)
    rc, out = run_ms(tmp_path, seed, ["--bake-discovery-cache"])
    assert rc == 0
    _, cache_raw = read_entry(out, CACHE_ENTRY)
    cache = json.loads(cache_raw.decode("utf-8"))
    assert not any(
        "__pycache__" in k or k.endswith(".pyc") for k in cache["files"]
    ), "pyc 工件不得入面摘要（kernel 走查同律排除，双侧才不失配）"
    assert "shared/llm/plugin.json" in cache["files"], "真源文件照常入摘要"
    assert "shared/llm/server.py" in cache["files"]
    # 幂等：摘要与 pyc 无关，含 pyc 种子二次烘焙零重写
    argv = ["materialize_seed.py", "--seed", out, "--out",
            str(tmp_path / "again.tar.zst"), "--bake-discovery-cache"]
    old = sys.argv
    sys.argv = argv
    try:
        rc2 = ms.main()
    finally:
        sys.argv = old
    assert rc2 == 0
    assert not os.path.exists(str(tmp_path / "again.tar.zst")), "pyc 在场也须零重写"
