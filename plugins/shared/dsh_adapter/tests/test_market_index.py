# @feature: FP-0.2.可观测性 dsh_adapter 插件 | @ci: python-coverage
"""index.json 发现源（设计 §4.1 GET index：官方 format 1 静态索引消费）。

契约：
1. 多源可配置：source = ``{"type": "url", "url": …}``（直连 format 1 JSON）
   或 ``{"type": "npm", "package": …, "version": …}``（registry tarball 抽
   index.json——npmmirror unpkg /files 无白名单 403 实证，tarball 可达）；
2. 缓存+不可达标记过期：抓取成功落缓存；失败→用缓存并 ``stale: true`` 带
   原因（禁静默）；缓存缺失且失败→该源 ``ok:false`` 且零条目；
3. 合并策略：按包名 first-source-wins（源顺序=优先级）；
4. format 1 形态（dsh-plugin-catalog@2026.1104.14928 实包实证）：
   ``{format:1, plugins:[{name, title, description{zh,en}, homepage, repository,
   metadata:{category}, versions:[{version, publishTimestamp, source:{type}, engines}]}]}``；
   版本挑选 = npm_semver.latest_version（发行版优先，禁 dist-tag）。

fetcher 全部注入（网络是外部依赖，单测零在线）；时序用注入 ``now``。
"""

from __future__ import annotations

import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import market_index as mi  # noqa: E402

pytestmark = pytest.mark.unit


def _format1_doc() -> dict:
    """format 1 最小真形态（字段名照实包）。"""
    return {
        "format": 1,
        "title": "catalog",
        "plugins": [
            {
                "name": "dsh-plugin-finance-data",
                "title": "demo/finance",
                "description": {"zh": "金融数据工具箱", "en": "Finance toolkit"},
                "metadata": {"category": "tools"},
                "versions": [
                    {"version": "0.3.1", "source": {"type": "npm"}, "engines": {}},
                    {"version": "0.3.2", "source": {"type": "npm"},
                     "engines": {"dsh": ">=0.2.0-rc.2 || >=0.2.1-rc.2"}},
                ],
            },
            {
                "name": "dsh-status-rotator",
                "title": "01Virex/status",
                "description": {"en": "Rotates status"},
                "metadata": {"category": "fun"},
                "versions": [{"version": "1.2.0-rc.1", "source": {"type": "git"}, "engines": {}}],
            },
        ],
    }


def _ok_fetcher(doc: dict | None = None, payload: bytes | None = None):
    body = payload if payload is not None else json.dumps(doc or _format1_doc()).encode("utf-8")
    calls: list[str] = []

    async def fetch(url: str, timeout_s: float) -> bytes:
        calls.append(url)
        return body

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def _npm_tarball_payload(doc: dict | None = None) -> bytes:
    """打包一个 registry tarball（package/index.json 形态）。"""
    doc = doc or _format1_doc()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = json.dumps(doc).encode("utf-8")
        info = tarfile.TarInfo("package/index.json")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class TestUrlSource:
    async def test_url_source_parses_format1_and_records_ok(self, tmp_path: Path) -> None:
        fetch = _ok_fetcher()
        out = await mi.fetch_market_index(
            sources=[{"type": "url", "url": "https://market.example/index.json"}],
            cache_dir=tmp_path, timeout_s=5, fetcher=fetch,
        )
        assert out["sources"][0]["ok"] is True
        assert out["sources"][0]["stale"] is False
        assert out["cached"] is False
        names = [p["name"] for p in out["plugins"]]
        assert names == ["dsh-plugin-finance-data", "dsh-status-rotator"]
        assert fetch.calls == ["https://market.example/index.json"]
        # 缓存落盘
        assert list(tmp_path.glob("*.json"))

    async def test_unreachable_uses_cache_and_marks_stale(self, tmp_path: Path) -> None:
        # 先成功一次落缓存
        await mi.fetch_market_index(
            sources=[{"type": "url", "url": "https://market.example/index.json"}],
            cache_dir=tmp_path, fetcher=_ok_fetcher(),
        )
        calls: list[str] = []

        async def failing(url: str, timeout_s: float) -> bytes:
            calls.append(url)
            raise OSError("connection reset")

        out = await mi.fetch_market_index(
            sources=[{"type": "url", "url": "https://market.example/index.json"}],
            cache_dir=tmp_path, fetcher=failing,
        )
        src = out["sources"][0]
        assert src["ok"] is False
        assert src["stale"] is True
        assert "connection reset" in (src["error"] or "")
        assert out["cached"] is True
        assert out["stale"] is True
        assert [p["name"] for p in out["plugins"]] == ["dsh-plugin-finance-data", "dsh-status-rotator"]
        assert len(calls) == 1  # 失败后不再重试同源（一次失败即降级缓存）

    async def test_failure_without_cache_is_honest_empty(self, tmp_path: Path) -> None:
        async def failing(url: str, timeout_s: float) -> bytes:
            raise OSError("unreachable")

        out = await mi.fetch_market_index(
            sources=[{"type": "url", "url": "https://x.example/index.json"}],
            cache_dir=tmp_path, fetcher=failing,
        )
        assert out["sources"][0]["ok"] is False
        assert out["sources"][0]["stale"] is False  # 从未有过缓存，谈不上过期
        assert out["plugins"] == []

    async def test_bad_json_is_recorded_as_error(self, tmp_path: Path) -> None:
        out = await mi.fetch_market_index(
            sources=[{"type": "url", "url": "https://x.example/index.json"}],
            cache_dir=tmp_path, fetcher=_ok_fetcher(payload=b"<html>not json</html>"),
        )
        assert out["sources"][0]["ok"] is False
        assert "format" in (out["sources"][0]["error"] or "").lower() or "json" in (out["sources"][0]["error"] or "").lower()


class TestNpmSource:
    async def test_tarball_source_extracts_index_json(self, tmp_path: Path) -> None:
        payload = _npm_tarball_payload()
        fetch = _ok_fetcher(payload=payload)
        out = await mi.fetch_market_index(
            sources=[{"type": "npm", "package": "dsh-plugin-catalog", "version": "2026.1104.14928"}],
            cache_dir=tmp_path, fetcher=fetch,
        )
        assert out["sources"][0]["ok"] is True
        assert [p["name"] for p in out["plugins"]][:2] == ["dsh-plugin-finance-data", "dsh-status-rotator"]
        url = fetch.calls[0]
        assert "dsh-plugin-catalog" in url  # 精确版本 tarball URL
        assert "2026.1104.14928" in url


class TestMerge:
    async def test_first_source_wins_per_name(self, tmp_path: Path) -> None:
        doc_a = _format1_doc()
        out = await mi.fetch_market_index(
            sources=[
                {"type": "url", "url": "https://a.example/index.json"},
                {"type": "url", "url": "https://b.example/index.json"},
            ],
            cache_dir=tmp_path,
            fetcher=_ok_fetcher(payload=json.dumps(doc_a).encode()),  # 简化：两源同文——合并按名去重
        )
        names = [p["name"] for p in out["plugins"]]
        assert len(names) == len(set(names))  # 按名去重（first-wins 语义）
        assert names.count("dsh-plugin-finance-data") == 1


class TestEntryLookup:
    def test_find_entry_and_latest_exact_version(self) -> None:
        doc = _format1_doc()
        entry = mi.find_entry(doc["plugins"], "dsh-plugin-finance-data")
        assert entry is not None
        # 发行版优先：0.3.2（release）> 0.3.1；engines 声明随版本走
        assert mi.latest_index_version(entry) == "0.3.2"
        assert mi.find_entry(doc["plugins"], "no-such") is None

    def test_prerelease_only_entry_picks_highest_prerelease(self) -> None:
        entry = {"name": "x", "versions": [
            {"version": "1.2.0-rc.1", "source": {"type": "git"}, "engines": {}},
            {"version": "1.2.0-rc.2", "source": {"type": "git"}, "engines": {}},
        ]}
        assert mi.latest_index_version(entry) == "1.2.0-rc.2"

    def test_engines_dsh_of_latest(self) -> None:
        entry = mi.find_entry(_format1_doc()["plugins"], "dsh-plugin-finance-data")
        assert mi.entry_engines_dsh(entry) == ">=0.2.0-rc.2 || >=0.2.1-rc.2"
        assert mi.entry_engines_dsh({"name": "x", "versions": []}) is None


class TestConfigSurface:
    def test_default_sources_are_npm_pinned(self) -> None:
        srcs = mi.default_sources()
        assert srcs, "默认发现源不得为空（管理页发现面可用性前提）"
        for s in srcs:
            assert s["type"] == "npm"
            assert s["version"]  # 精确版本纪律

    def test_config_overrides_sources(self) -> None:
        cfg = {"market": {"sources": [{"type": "url", "url": "https://m.example/index.json"}]}}
        assert mi.sources_from_config(cfg) == cfg["market"]["sources"]

    def test_config_missing_returns_defaults(self) -> None:
        assert mi.sources_from_config({}) == mi.default_sources()

    async def test_age_staleness_marks_without_refetch_failure(self, tmp_path: Path) -> None:
        await mi.fetch_market_index(
            sources=[{"type": "url", "url": "https://a.example/index.json"}],
            cache_dir=tmp_path, fetcher=_ok_fetcher(), now=1000.0,
        )
        # 缓存太老（>max_age）→ 即使本次抓取成功也重抓；模拟：抓取失败 + 缓存超龄 → stale
        async def failing(url: str, timeout_s: float) -> bytes:
            raise OSError("down")

        out = await mi.fetch_market_index(
            sources=[{"type": "url", "url": "https://a.example/index.json"}],
            cache_dir=tmp_path, fetcher=failing, now=1000.0 + 25 * 3600,
        )
        assert out["sources"][0]["stale"] is True
        assert out["sources"][0]["fetched_at"] == pytest.approx(1000.0)  # 沿用缓存时间戳
