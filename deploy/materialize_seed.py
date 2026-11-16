#!/usr/bin/env python3
"""种子打包期物化+烘焙：hardlink 物化 + etc/resolv.conf 写定 + venv 装配后布局预烤
+ 插件发现缓存预烤。

背景（2026-10-31/11-01 真机实证，docs/working/真机安装即用专项_20261101.md）：
- 跨 venv 硬链接去重产物在 Android untrusted_app 域不可 link()（SELinux 拒绝），
  seedx 首启解包必炸 → hardlink 条目物化为独立实体副本（拷目标内容保权限）。
- etc/resolv.conf 原值 8.8.8.8 大陆网络不可达 → 写定国内公共 DNS。
- venv 装配原为设备端 mobile_init.sh 运行期动作（真机 17 个 ≈1-2min）→ 设计
  公理「能在打包期做好的就在打包期做好」：本脚本在种子内直接产出装配后布局
  opt/agentos/plugin-venvs/<plugin_id>/{bin/python,bin/python3 符号链接,
  pyvenv.cfg,lib/python3.12/site-packages}，设备端零装配；opt/agentos/venvs
  原快照保留（回滚兼容）。登记键 = plugin.json id（缺失回退目录名），与
  mobile_init.sh 同律；bin 符号链接与 pyvenv.cfg 内容与 mobile_init.sh 逐字
  同构（home/executable 指向种子内 PBS cpython 基座）。
- 插件发现缓存（T4/R-2 层 2，--bake-discovery-cache）：survey 已扫全部
  plugin.json → 顺手产出 opt/agentos/plugins/shared/discovery-cache.json
  （插件面逐文件 sha256 + 候选表），内核 boot 对账全匹配即跳过现场扫描重解与
  G2 spawn 复验（格式契约见台账 T4 第一刀设计节）。

幂等：无 hardlink、resolv 已写定、plugin-venvs 齐备、无重名条目、（带
--bake-discovery-cache 时）发现缓存现物与再生成字节全等 → 二次运行零变化。
重名条目是 tar 语义缺陷（解包器 last-wins 覆盖，时序歧义）：重写遍
恒定首见优先去重（2026-11-01 实测历史脏种子 45,730 条烘焙位重复，字节级
一致，清洗安全）。
两遍架构：pass1 解压裸 tar survey（条目索引 + plugin.json id 映射 + venv 快照
清单 + 插件面索引），pass2 流式重写（物化 + 写定 + 烘焙发射 + 缓存发射 + 去重）。
"""

import argparse
import hashlib
import io
import json
import os
import sys
import tarfile
from pathlib import Path

import zstandard as zstd

RESOLV_ENTRY = "etc/resolv.conf"
VENV_PREFIX = "opt/agentos/venvs/"
MANIFEST_PREFIX = "opt/agentos/plugins/shared/"
BAKED_PREFIX = "opt/agentos/plugin-venvs/"
SITE_SUB = "lib/python3.12/site-packages"
PY_HOME = "/opt/agentos/python"
PY_BASE = PY_HOME + "/bin/python3.12"
PY_VERSION = "3.12.15"

# ── 发现缓存预烤（T4/R-2 层 2，2026-11-01）──
# 缓存条目位 = 内核默认探测位 <AGENTOS_PLUGINS_DIR>/discovery-cache.json
# （start_stack.sh 设 AGENTOS_PLUGINS_DIR=/opt/agentos/plugins/shared）。
PLUGIN_FACE_PREFIX = "opt/agentos/plugins/"
CACHE_ENTRY = "opt/agentos/plugins/shared/discovery-cache.json"
CACHE_VERSION = 1
MANIFEST_BASENAMES = ("plugin.json", "plugin.yaml")

# 注册键 = plugin.json id（缺失/损坏回退目录名，与 mobile_init.sh 同律）。

# 可派生 Python 工件（T4 设备轮实证排除，两侧同律）：解释器运行期在插件面内
# 写 __pycache__/*.pyc，boot 间即新于缓存——非漂移，不入摘要不比对。除此类外
# 暂不扩其他运行期写入类别（.log/.lock 等，需求第一；台账留观察项）。
PYCACHE_DIRNAME = "__pycache__"
PYC_SUFFIX = ".pyc"


def is_pycache_artifact(entry_name: str) -> bool:
    """条目是否为可派生 Python 工件（路径任一段为 __pycache__，或 basename
    以 .pyc 结尾——kernel 侧 walk_face 同规则）。"""
    if entry_name.rpartition("/")[2].endswith(PYC_SUFFIX):
        return True
    return any(seg == PYCACHE_DIRNAME for seg in entry_name.split("/"))


def pyvenv_cfg() -> bytes:
    return (
        f"home = {PY_HOME}\nversion = {PY_VERSION}\ninclude-system-site-packages = false\nexecutable = {PY_BASE}\n"
    ).encode()


def stream_entries_raw(path):
    with (
        open(path, "rb") as fh,
        tarfile.open(fileobj=io.BufferedReader(fh, buffer_size=1 << 20), mode="r|") as tf,
    ):
        while True:
            member = tf.next()
            if member is None:
                return
            yield member, tf


def manifest_id(raw: bytes) -> str:
    try:
        return json.loads(raw.decode("utf-8")).get("id") or ""
    except Exception:
        return ""


def survey(path):
    # path=裸 tar（解压产物），直读不走 zstd
    total = 0
    hardlinks = 0
    snapshots = 0
    dups = 0
    seen = set()
    regulars = {}
    resolv_content = None
    plugin_ids = {}
    venv_dirs = set()
    # 插件面索引（发现缓存烘焙输入）：常规/符号链接/硬链接分类 + manifest 目录表
    face_regulars = {}  # 条目名 -> (offset_data, size)
    face_links = {}  # 条目名 -> linkname
    face_hardlinks = {}  # 条目名 -> linkname（摘要经 index 解析目标内容；不可解析拒烘）
    manifest_dirs: dict[str, str] = {}  # manifest 所在目录（不含尾斜杠）-> manifest 文件名（首见）
    cache_content = None  # 缓存条目现物（幂等判定用）
    for member, tf in stream_entries_raw(path):
        total += 1
        name = member.name
        if name in seen:
            dups += 1
        else:
            seen.add(name)
        if name.startswith(VENV_PREFIX):
            snapshots += 1
        if member.type == b"1":
            hardlinks += 1
            if name.startswith(PLUGIN_FACE_PREFIX) and name != CACHE_ENTRY:
                face_hardlinks[name] = member.linkname
        elif member.type == b"0":
            if name == RESOLV_ENTRY:
                resolv_content = tf.extractfile(member).read()
            elif name == CACHE_ENTRY:
                cache_content = tf.extractfile(member).read()
            elif name.startswith(PLUGIN_FACE_PREFIX):
                base = name.rpartition("/")[2]
                if base in MANIFEST_BASENAMES:
                    manifest_dirs.setdefault(name.rpartition("/")[0], base)
            if name.startswith(PLUGIN_FACE_PREFIX) and name != CACHE_ENTRY and not is_pycache_artifact(name):
                face_regulars[name] = (member.offset_data, member.size)
            if name.startswith(MANIFEST_PREFIX) and name != CACHE_ENTRY:
                rest = name[len(MANIFEST_PREFIX) :]
                d, sep, tail = rest.partition("/")
                if sep and tail == "plugin.json" and d not in plugin_ids:
                    plugin_ids[d] = manifest_id(tf.extractfile(member).read())
            regulars[name] = (member.offset_data, member.size)
        elif member.type == b"2":
            if name.startswith(PLUGIN_FACE_PREFIX) and name != CACHE_ENTRY and not is_pycache_artifact(name):
                face_links[name] = member.linkname
        if name.startswith(VENV_PREFIX):
            rest = name[len(VENV_PREFIX) :]
            d, sep, tail = rest.partition("/")
            if sep and (tail == SITE_SUB or tail.startswith(SITE_SUB + "/")):
                venv_dirs.add(d)
    return (
        total,
        hardlinks,
        snapshots,
        dups,
        regulars,
        resolv_content,
        plugin_ids,
        venv_dirs,
        face_regulars,
        face_links,
        face_hardlinks,
        manifest_dirs,
        cache_content,
    )


def compute_face_digests(raw, face_regulars, face_hardlinks, regulars):
    """插件面常规文件摘要：seek+offset_data 独立重读（流式 tf.fileobj 只许被
    survey 原位消费，二次读取撞条目头——与烘焙位同款防串读）。

    硬链接条目（typeflag '1'）无内嵌数据（size 0 + linkname）——直读必零字节
    失真（2026-11-01 全量链接线实锤：pack dedup 后 sdk 面 64 条硬链接拒烘）。
    摘要经 survey 产物 index（regulars[linkname]=(offset,size)）解析目标真实
    内容计算——主流程物化后该路径即此字节，硬链接态与物化态烘焙字节恒一致。
    目标不可解析者不产摘要，由 build_discovery_cache 拒烘（fail-closed 保留给
    这类真异常）。"""
    digests = {}
    with open(raw, "rb") as seek_fh:
        for name, (off, size) in face_regulars.items():
            seek_fh.seek(off)
            digests[name] = hashlib.sha256(seek_fh.read(size)).hexdigest()
        for name, linkname in face_hardlinks.items():
            target = regulars.get(linkname)
            if target is None:
                continue  # 真异常：拒烘判定在 build_discovery_cache（链不可达）
            off, size = target
            seek_fh.seek(off)
            digests[name] = hashlib.sha256(seek_fh.read(size)).hexdigest()
    return digests


def build_discovery_cache(face_regulars, face_links, face_hardlinks, manifest_dirs, digests):
    """发现缓存工件字节（格式契约见台账 T4 第一刀设计节）。

    返回 (bytes, None) 或 (None, 拒烘原因)。字节确定性 = sort_keys + 紧凑分隔符
    + 尾换行——幂等判定按与种子内现物字节全等；硬链接条目摘要=目标内容
    （compute_face_digests 经 index 解析），硬链接态与物化态产出字节恒一致。"""
    unresolved = sorted(n for n, link in face_hardlinks.items() if n not in digests)
    if unresolved:
        return None, (
            f"插件面硬链接条目目标不可解析（index 无此常规条目，真异常拒烘，"
            f"首个: {unresolved[0]}，共 {len(unresolved)}）"
        )
    plugin_dirs: dict[str, dict[str, str]] = {}
    for pdir, fname in manifest_dirs.items():
        root, _, dname = pdir.rpartition("/")
        plugin_dirs.setdefault("/" + root, {})[dname] = fname
    face_cut = len(PLUGIN_FACE_PREFIX)
    cache = {
        "version": CACHE_VERSION,
        "generator": "materialize_seed.py",
        "face_root": "/" + PLUGIN_FACE_PREFIX.rstrip("/"),
        "files": {rel[face_cut:]: digests[rel] for rel in sorted(digests)},
        "links": {rel[face_cut:]: face_links[rel] for rel in sorted(face_links)},
        "plugin_dirs": {root: dict(sorted(plugin_dirs[root].items())) for root in sorted(plugin_dirs)},
    }
    text = json.dumps(cache, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return (text + "\n").encode("utf-8"), None


def _renamed(member, new_name):
    info = tarfile.TarInfo(new_name)
    info.type = member.type
    info.size = member.size
    info.mode = member.mode
    info.mtime = member.mtime
    info.uid = member.uid
    info.gid = member.gid
    info.uname = member.uname
    info.gname = member.gname
    info.linkname = member.linkname
    return info


def _emit_scaffold(out, key, stats):
    """装配面骨架：全层级目录条目 + bin 双符号链接 + pyvenv.cfg。

    目录条目显式铺齐到 site-packages 根——解包器（tar-rs）对符号链接条目
    是否自动建父目录不可假定，目录先行即零依赖；种子部署树 opt/agentos/
    已由 ubuntu/overlay 条目承载，base 及以下为本脚本新增命名空间。"""
    base = BAKED_PREFIX + key
    if not stats["baked_root"]:
        root = tarfile.TarInfo(BAKED_PREFIX.rstrip("/"))
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        root.mtime = 0
        out.addfile(root)
        stats["baked_root"] = True
    for sub in ("", "bin", "lib", "lib/python3.12", SITE_SUB):
        info = tarfile.TarInfo(base + "/" + sub if sub else base)
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        info.mtime = 0
        out.addfile(info)
    for link_name in ("bin/python", "bin/python3"):
        info = tarfile.TarInfo(f"{base}/{link_name}")
        info.type = tarfile.SYMTYPE
        info.linkname = PY_BASE
        info.mode = 0o777
        info.mtime = 0
        out.addfile(info)
    cfg = pyvenv_cfg()
    info = tarfile.TarInfo(f"{base}/pyvenv.cfg")
    info.type = tarfile.REGTYPE
    info.size = len(cfg)
    info.mode = 0o644
    info.mtime = 0
    out.addfile(info, io.BytesIO(cfg))
    stats["baked_venvs"] += 1


def _emit_baked(out, member, baked_name, seek_fh, index, stats):
    """装配位发射：目录/符号链接/常规文件逐条目换名保真，硬链接物化为实体。

    数据一律经 seek_fh 按 offset_data 定位重读——流式 tf.fileobj 只许被原位
    透传消费一次，烘焙位的二次读取若再走它会撞上下一条目头（unexpected end
    of data，本轮离线自证实测）。"""
    if member.type == b"1":
        off, size = index[member.linkname]
        info = tarfile.TarInfo(baked_name)
        info.type = tarfile.REGTYPE
        info.size = size
        info.mode = member.mode
        info.mtime = member.mtime
        info.uid = member.uid
        info.gid = member.gid
        info.uname = member.uname
        info.gname = member.gname
        seek_fh.seek(off)
        out.addfile(info, seek_fh)
    elif member.isfile():
        info = _renamed(member, baked_name)
        seek_fh.seek(member.offset_data)
        out.addfile(info, seek_fh)
    else:
        # 目录/符号链接零数据落位（linkname 已随 _renamed 保真）
        out.addfile(_renamed(member, baked_name))
    stats["baked_entries"] += 1


def rewrite(
    raw, dst, index, dns_nameservers, plugin_ids, venv_dirs, replaces, baked_ok, drop_snapshots, cache_bytes=None
):
    resolv_new = "".join("nameserver " + s + chr(10) for s in dns_nameservers).encode()
    stats = {
        "materialized": 0,
        "resolv_rewritten": 0,
        "baked_venvs": 0,
        "baked_entries": 0,
        "baked_root": False,
        "replaced": 0,
        "snapshots_dropped": 0,
        "deduped": 0,
        "cache_baked": 0,
    }
    # 烘焙发射位记账：每 venv 骨架只发一次（键=venv 目录名；键序随首条目，确定性）
    # baked_ok（输入已含烘焙位）= 视为发射齐全（烘焙条目走通用透传，快照按需丢弃）
    baked_emitted = set(venv_dirs) if baked_ok else set()
    # 发现缓存发射记账：输入在位条目=原位换血（防重名残留），缺席=尾追加
    cache_emitted = False
    # 首见优先去重：重名条目只保留首个（历史脏种子实测重复份字节级一致；
    # 本发射面（骨架/烘焙位）各键只发一次，与透传去重互不干扰）
    emitted = set()
    with open(raw, "rb") as seek_fh, open(dst + ".tmp", "wb") as out_f:
        cctx = zstd.ZstdCompressor(level=3)
        with cctx.stream_writer(out_f) as comp, tarfile.open(fileobj=comp, mode="w|", format=tarfile.GNU_FORMAT) as out:
            for member, _ in stream_entries_raw(raw):
                name = member.name
                if baked_ok and drop_snapshots and name.startswith(VENV_PREFIX):
                    stats["snapshots_dropped"] += 1
                    continue
                if cache_bytes is not None and name == CACHE_ENTRY:
                    if not cache_emitted:
                        info = tarfile.TarInfo(CACHE_ENTRY)
                        info.type = tarfile.REGTYPE
                        info.size = len(cache_bytes)
                        info.mode = 0o644
                        info.mtime = 0
                        out.addfile(info, io.BytesIO(cache_bytes))
                        stats["cache_baked"] += 1
                        cache_emitted = True
                    continue
                if name in emitted:
                    stats["deduped"] += 1
                    continue
                emitted.add(name)
                if member.type == b"1":
                    off, size = index[member.linkname]
                    info = tarfile.TarInfo(member.name)
                    info.type = tarfile.REGTYPE
                    info.size = size
                    info.mode = member.mode
                    info.mtime = member.mtime
                    info.uid = member.uid
                    info.gid = member.gid
                    info.uname = member.uname
                    info.gname = member.gname
                    seek_fh.seek(off)
                    out.addfile(info, seek_fh)
                    stats["materialized"] += 1
                elif member.type == b"0" and member.name == RESOLV_ENTRY:
                    info = tarfile.TarInfo(member.name)
                    info.type = tarfile.REGTYPE
                    info.size = len(resolv_new)
                    info.mode = member.mode
                    info.mtime = member.mtime
                    out.addfile(info, io.BytesIO(resolv_new))
                    stats["resolv_rewritten"] += 1
                elif member.type == b"0" and member.name in replaces:
                    # 条目替换（如内核换刀）：内容来自宿主文件，元数据随原条目
                    host_path = replaces[member.name]
                    info = tarfile.TarInfo(member.name)
                    info.type = tarfile.REGTYPE
                    info.size = Path(host_path).stat().st_size
                    info.mode = member.mode
                    info.mtime = member.mtime
                    with open(host_path, "rb") as hf:
                        out.addfile(info, hf)
                    stats["replaced"] += 1
                elif member.type == b"0":
                    # 数据经 seek_fh 按 offset_data 定位（tf.fileobj 留给流式
                    # next() 自行相对跳过——同一成员的数据烘焙位还要再读一次）
                    seek_fh.seek(member.offset_data)
                    out.addfile(member, seek_fh)
                else:
                    out.addfile(member)
                # 烘焙（正交于上方面位）：venv 快照 site-packages → 装配后布局
                # （输入已含烘焙位=baked_ok 时跳过发射，防重复条目）
                name = member.name
                if not baked_ok and name.startswith(VENV_PREFIX):
                    rest = name[len(VENV_PREFIX) :]
                    vdir, sep, _tail = rest.partition("/")
                    if sep:
                        key = plugin_ids.get(vdir) or vdir
                        if vdir not in baked_emitted:
                            _emit_scaffold(out, key, stats)
                            baked_emitted.add(vdir)
                        tail = _tail
                        if tail == SITE_SUB or tail.startswith(SITE_SUB + "/"):
                            baked_name = BAKED_PREFIX + key + "/" + SITE_SUB + tail[len(SITE_SUB) :]
                            _emit_baked(out, member, baked_name, seek_fh, index, stats)
            # 发现缓存尾追加（输入缺席时；条目在位已原位换血）
            if cache_bytes is not None and not cache_emitted:
                info = tarfile.TarInfo(CACHE_ENTRY)
                info.type = tarfile.REGTYPE
                info.size = len(cache_bytes)
                info.mode = 0o644
                info.mtime = 0
                out.addfile(info, io.BytesIO(cache_bytes))
                stats["cache_baked"] += 1
                cache_emitted = True
            out.close()
    missing = venv_dirs - baked_emitted
    if missing:
        # 快照清单在 survey 有、重写遍零条目 = 种子自相矛盾，拦截不静默
        raise SystemExit(f"[materialize][ERROR] venv 快照缺失条目: {sorted(missing)}")
    Path(dst + ".tmp").replace(dst)
    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", required=True)
    ap.add_argument("--dns", default="223.5.5.5,119.29.29.29")
    ap.add_argument("--out", default=None)
    ap.add_argument(
        "--replace",
        action="append",
        default=[],
        metavar="GUEST=HOST",
        help="条目替换：种子内 guest 路径内容替换为宿主文件（可重复，如内核换刀）",
    )
    ap.add_argument("--check", action="store_true")
    ap.add_argument(
        "--drop-snapshots", action="store_true", help="预烤在位时丢弃 venv 快照条目（体积减半；仅 baked_ok 时生效）"
    )
    ap.add_argument(
        "--bake-discovery-cache",
        action="store_true",
        help="烘焙插件发现缓存 discovery-cache.json（T4/R-2 层 2：内核"
        "boot 对账通过即跳过扫描与 G2 复验；幂等判定含缓存现物全等）",
    )
    args = ap.parse_args()
    dns = [n.strip() for n in args.dns.split(",") if n.strip()]
    drop_snapshots = args.drop_snapshots
    if not dns:
        print("[materialize] FAIL --dns empty", file=sys.stderr)
        return 3
    raw = os.path.join(os.path.dirname(os.path.abspath(args.out or args.seed)), ".seed_raw.tar")
    with open(args.seed, "rb") as fh, open(raw, "wb") as out:
        zstd.ZstdDecompressor().copy_stream(fh, out)
    (
        total,
        hardlinks,
        snapshots,
        dups,
        regulars,
        resolv_content,
        plugin_ids,
        venv_dirs,
        face_regulars,
        face_links,
        face_hardlinks,
        manifest_dirs,
        cache_content,
    ) = survey(raw)
    want = "".join("nameserver " + s + chr(10) for s in dns).encode()
    resolv_ok = resolv_content == want
    baked_ok = all((BAKED_PREFIX + (plugin_ids.get(v) or v) + "/pyvenv.cfg") in regulars for v in venv_dirs)
    bake_cache = args.bake_discovery_cache
    cache_bytes = None
    cache_ok = False
    if bake_cache:
        digests = compute_face_digests(raw, face_regulars, face_hardlinks, regulars)
        cache_bytes, cache_err = build_discovery_cache(
            face_regulars, face_links, face_hardlinks, manifest_dirs, digests
        )
        if cache_bytes is None:
            print(f"[materialize] FAIL --bake-discovery-cache: {cache_err}", file=sys.stderr)
            return 3
        cache_ok = cache_content == cache_bytes
    replaces = {}
    for r in args.replace:
        guest, sep, host = r.partition("=")
        if not sep or not guest or not os.path.isfile(host):
            print(f"[materialize] FAIL --replace 非法或宿主文件缺失: {r}", file=sys.stderr)
            return 3
        if guest not in regulars:
            print(f"[materialize] FAIL --replace 目标条目不在种子内: {guest}", file=sys.stderr)
            return 3
        replaces[guest] = host
    replaces_ok = all(regulars[g][1] == Path(h) for g, h in replaces.items())
    print(
        f"[materialize] survey: entries={total} hardlinks={hardlinks} "
        f"resolv_match={resolv_ok} venvs={len(venv_dirs)} baked={baked_ok} "
        f"face_files={len(face_regulars)} face_links={len(face_links)} "
        f"cache={'match' if cache_ok else ('pending' if bake_cache else 'off')} "
        f"dups={dups}"
    )
    snapshots_absent = snapshots == 0
    if (
        hardlinks == 0
        and resolv_ok
        and baked_ok
        and replaces_ok
        and dups == 0
        and (not drop_snapshots or snapshots_absent)
        and (not bake_cache or cache_ok)
    ):
        print("[materialize] already-materialized+baked, zero rewrite")
        return 0
    if drop_snapshots and not baked_ok:
        print("[materialize] WARN --drop-snapshots 需预烤在位（baked_ok=false），本轮忽略")
    if args.check:
        print("[materialize] CHECK: pending, not rewritten")
        return 0
    dst = args.out or args.seed
    stats = rewrite(
        raw,
        dst,
        regulars,
        dns,
        plugin_ids,
        venv_dirs,
        replaces,
        baked_ok,
        drop_snapshots,
        cache_bytes if bake_cache else None,
    )
    os.unlink(raw)
    print(
        f"[materialize] done: materialized={stats['materialized']} "
        f"resolv_rewritten={stats['resolv_rewritten']} "
        f"baked_venvs={stats['baked_venvs']} baked_entries={stats['baked_entries']} "
        f"replaced={stats['replaced']} snapshots_dropped={stats['snapshots_dropped']} "
        f"cache_baked={stats['cache_baked']} "
        f"deduped={stats['deduped']} -> {dst}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
