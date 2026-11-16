"""index.json 发现源（设计 §4.1 GET index：官方 format 1 静态索引消费）。

官方不出索引（〔调研〕§2.1："DSH ships no index source"），第三方市场站
喂官方开放的 format 1 静态索引；生态里另有 npm 目录包（dsh-plugin-catalog，
4481 条，format:1 实包实证）。本模块消费两类源：

- ``{"type": "url", "url": …}``：直连 format 1 JSON 的市场站索引；
- ``{"type": "npm", "package": …, "version": …}``：registry tarball 抽
  ``package/index.json``（npmmirror unpkg /files 无白名单 403 实证——
  2026-11-04；registry tarball 稳定可达且合 npm 纪律）。

发现与安装解耦（设计 §4.1）：发现面只读索引不执行代码；缓存不可达降级
（stale 标记 + 失败原因，禁静默）；多源按包名 first-wins（源顺序=优先级）。
版本挑选 = npm_semver.latest_version（发行版优先，禁 dist-tag——npm latest
陈旧已实证〔调研〕§2.2）。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import tarfile
import time
from pathlib import Path
from typing import Any

from npm_semver import latest_version

logger = logging.getLogger(__name__)

# 默认发现源：npm 目录包精确版本（npm 纪律；升级 = 换 version 字段）。
# dsh-plugin-catalog = 生态目录聚合（2026-11-04 实包：format 1，4481 条）。
DEFAULT_MARKET_SOURCES: list[dict[str, str]] = [
    {"type": "npm", "package": "dsh-plugin-catalog", "version": "2026.1104.14928"},
]
DEFAULT_FETCH_TIMEOUT_S = 30.0

CacheKey = str


def default_sources() -> list[dict[str, str]]:
    return [dict(s) for s in DEFAULT_MARKET_SOURCES]


def sources_from_config(config: dict[str, Any] | None) -> list[dict[str, str]]:
    """配置面 ``market.sources``（多源可配置）；缺省/空回默认源。"""
    market = (config or {}).get("market")
    if isinstance(market, dict):
        sources = market.get("sources")
        if isinstance(sources, list) and sources:
            return [s for s in sources if isinstance(s, dict)]
    return default_sources()


def _source_key(source: dict[str, Any]) -> str:
    if source.get("type") == "npm":
        raw = f"npm:{source.get('package')}@{source.get('version')}"
    else:
        raw = f"url:{source.get('url')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _tarball_url(package: str, version: str) -> str:
    return f"https://registry.npmmirror.com/{package}/-/{package.replace('@', '').replace('/', '-')}-{version}.tgz"


def _source_url(source: dict[str, Any]) -> str:
    if source.get("type") == "npm":
        return _tarball_url(str(source.get("package")), str(source.get("version")))
    return str(source.get("url"))


def _parse_index(payload: bytes, source_label: str) -> dict[str, Any]:
    """bytes → format 1 文档；形态不符抛 ValueError（fail-visible）。"""
    try:
        doc = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"{source_label}: not valid JSON ({exc})") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("plugins"), list):
        raise ValueError(f"{source_label}: not a format-1 index (missing plugins[] )")
    return doc


def _extract_tarball_index(payload: bytes, source_label: str) -> bytes:
    """registry tarball → ``package/index.json`` 字节。"""
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tf:
            for member in tf.getmembers():
                if member.name == "package/index.json" or member.name.endswith("/index.json"):
                    fh = tf.extractfile(member)
                    if fh is not None:
                        return fh.read()
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise ValueError(f"{source_label}: bad tarball ({exc})") from exc
    raise ValueError(f"{source_label}: index.json not found in tarball")


def _default_fetcher() -> Any:
    async def fetch(url: str, timeout_s: float) -> bytes:
        def _read() -> bytes:
            import urllib.request

            req = urllib.request.Request(url, headers={"User-Agent": "agentos-dsh-adapter"})
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return resp.read()

        return await asyncio.to_thread(_read, )

    return fetch


def _cache_path(cache_dir: Path, key: CacheKey) -> Path:
    return cache_dir / f"idx-{key}.json"


def _read_cache(cache_dir: Path, key: CacheKey) -> dict[str, Any] | None:
    p = _cache_path(cache_dir, key)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("[dsh_market] 缓存读取失败（忽略）: %s | %s", p, exc)
        return None
    return data if isinstance(data, dict) and isinstance(data.get("plugins"), list) else None


def _write_cache(cache_dir: Path, key: CacheKey, doc: dict[str, Any], now: float) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = _cache_path(cache_dir, key)
    p.write_text(json.dumps({"fetched_at": now, "plugins": doc.get("plugins", [])},
                            ensure_ascii=False), encoding="utf-8")


async def fetch_market_index(
    sources: list[dict[str, Any]],
    cache_dir: str | Path,
    timeout_s: float = DEFAULT_FETCH_TIMEOUT_S,
    fetcher: Any = None,
    now: float | None = None,
) -> dict[str, Any]:
    """抓取并合并多源 format 1 索引（不可达 → 缓存 + stale 标记）。

    Returns:
        ``{sources: [{name, ok, stale, error?, fetched_at}], plugins: […],
        cached: bool, stale: bool}``
    """
    now = time.time() if now is None else now
    fetch = fetcher if fetcher is not None else _default_fetcher()
    cache_dir = Path(cache_dir)
    reports: list[dict[str, Any]] = []
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    any_cached = False
    any_stale = False
    for source in sources:
        key = _source_key(source)
        label = (_source_url(source) or "?")[:120]
        report: dict[str, Any] = {"name": label, "ok": False, "stale": False,
                                  "error": None, "fetched_at": None}
        plugins: list[dict[str, Any]] | None = None
        try:
            payload = await fetch(_source_url(source), timeout_s)
            if source.get("type") == "npm":
                payload = _extract_tarball_index(payload, label)
            doc = _parse_index(payload, label)
            plugins = doc["plugins"]
            _write_cache(cache_dir, key, doc, now)
            report["ok"] = True
            report["fetched_at"] = now
        except (OSError, ValueError, TimeoutError) as exc:
            report["error"] = str(exc)
            cached = _read_cache(cache_dir, key)
            if cached is not None:
                plugins = cached["plugins"]
                fetched_at = float(cached.get("fetched_at") or 0)
                report["fetched_at"] = fetched_at
                # 抓取失败 = 缓存态不可信：无论年龄一律标记过期（禁静默续用）
                report["stale"] = True
                any_cached = True
        if plugins:
            for entry in plugins:
                if not isinstance(entry, dict) or not entry.get("name"):
                    continue
                name = str(entry["name"])
                if name not in merged:  # first-source-wins（源顺序=优先级）
                    merged[name] = entry
                    order.append(name)
        reports.append(report)
        if report["stale"]:
            any_stale = True
    return {
        "sources": reports,
        "plugins": [merged[name] for name in order],
        "cached": any_cached,
        "stale": any_stale,
    }


def find_entry(plugins: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for entry in plugins:
        if isinstance(entry, dict) and str(entry.get("name")) == name:
            return entry
    return None


def latest_index_version(entry: dict[str, Any] | None) -> str | None:
    """条目 → 最新精确版本（发行版优先；禁 dist-tag 语义，npm_semver 裁决）。"""
    if not entry:
        return None
    versions = [str(v.get("version")) for v in entry.get("versions", []) if isinstance(v, dict)]
    return latest_version(versions)


def entry_engines_dsh(entry: dict[str, Any] | None) -> str | None:
    """条目最新精确版本的 ``engines.dsh`` 声明；无版本/无声明返回 None。"""
    if not entry:
        return None
    versions = [v for v in entry.get("versions", []) if isinstance(v, dict) and v.get("version")]
    best = latest_version([str(v.get("version")) for v in versions])
    for v in versions:
        if str(v.get("version")) == best:
            engines = v.get("engines")
            if isinstance(engines, dict) and isinstance(engines.get("dsh"), str):
                return engines["dsh"]
            return None
    return None
