# @feature: FP-0.2.MCP OAuth 授权 | @vision: V3 可嵌入 | @ci: python-coverage
"""``guidance`` 引导面：仓内真配置（``config/plugins/oauth_broker/guidance.yaml``）自证 + 行容错。

流程测试里的 ``_GUIDANCE_CONFIG`` 是测试镜像（只证构造逻辑）；本文件读**仓内真文件**，
证明「新增一个服务 = 加一行配置」这条承诺在真配置面上成立（URL 全在配置，代码零服务知识）。
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import flow
import guidance
import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SHIPPED_CONFIG = _REPO_ROOT / "config" / "plugins" / "oauth_broker" / "guidance.yaml"
_CALLBACK = "http://127.0.0.1:9100/ext/oauth_broker/callback"


def _shipped_providers() -> dict[str, guidance.ProviderGuide]:
    # config_files id=guidance → 注入面按 id 命名空间嵌套（plugin.get_config()["guidance"]）
    return guidance.load_providers(yaml.safe_load(_SHIPPED_CONFIG.read_text(encoding="utf-8"))["guidance"])


def test_shipped_config_builds_github_pages_with_values() -> None:
    """真配置面：github.com 行 → 注册页/PAT 页 URL 带上逐项替换后的值。"""
    provider, row = guidance.match_provider(_shipped_providers(), hosts=["github.com"])

    assert provider == "github.com"
    guide = guidance.build_guide(
        provider=provider,
        guide=row,
        callback_url=_CALLBACK,
        homepage_url="http://127.0.0.1:9100",
        scopes=["repo", "read:org"],
        action=flow.GUIDANCE_ACTION_CREATE_PAT,
        access_token_key="MCP_OAUTH_GITHUB_COM_ACCESS_TOKEN",
    )

    registration = parse_qs(urlsplit(guide["registration_url"]).query)
    assert registration["oauth_application[callback_url]"] == [_CALLBACK], "回调地址取真实回调 URL"
    assert registration["oauth_application[name]"] == [guide["app_name"]]
    assert registration["oauth_application[url]"] == [guide["homepage_url"]]
    pat_page = parse_qs(urlsplit(guide["pat_page_url"]).query)
    assert pat_page["scopes"] == ["repo,read:org"], "scope 清单来自 PRM，逗号形态进 PAT 页"
    assert guide["registration_prefilled"] is True
    assert guide["pat_page_prefilled"] is True
    assert guide["pat_env_key"] == "MCP_OAUTH_GITHUB_COM_ACCESS_TOKEN"
    assert guide["auth_config_snippet"] == '{"type":"bearer","value":"${MCP_OAUTH_GITHUB_COM_ACCESS_TOKEN}"}'


def test_load_providers_ignores_bad_rows_and_accepts_flat_form() -> None:
    """坏行忽略（非字典行 / 空 host），两种配置形态（``providers`` 包裹与平铺）都认。"""
    wrapped = guidance.load_providers(
        {"providers": {"ok.example": {}, "": {"token_url": "https://x.example/new"}, "bad": "not-a-dict"}}
    )
    flat = guidance.load_providers({"flat.example": {"registration_url": "https://flat.example/new"}})

    assert set(wrapped) == {"ok.example"}
    assert wrapped["ok.example"].app_name == guidance.DEFAULT_APP_NAME
    assert wrapped["ok.example"].registration_url is None
    assert flat["flat.example"].registration_url == "https://flat.example/new"


def test_match_provider_skips_blank_hosts_and_host_of_none() -> None:
    """匹配面跳过空 host（None/空串不炸）；``host_of(None)`` 回 None（无 URL 可解析）。"""
    providers = guidance.load_providers({"providers": {"hit.example": {}}})

    assert guidance.match_provider(providers, hosts=[None, "", "hit.example"])[0] == "hit.example"
    assert guidance.match_provider(providers, hosts=[None, ""]) == (None, guidance.ProviderGuide())
    assert guidance.host_of(None) is None


def test_build_guide_returns_plain_url_when_row_has_no_params() -> None:
    """行只给 URL 没给预填参数 → URL 原样返回、prefilled=False（不空拼一个 '?'）。"""
    guide = guidance.build_guide(
        provider="plain.example",
        guide=guidance.ProviderGuide(registration_url="https://plain.example/new"),
        callback_url=_CALLBACK,
        homepage_url="http://127.0.0.1:9100",
        scopes=[],
        action=flow.GUIDANCE_ACTION_REGISTER_APP,
        access_token_key="MCP_OAUTH_PLAIN_EXAMPLE_ACCESS_TOKEN",
    )

    assert guide["registration_url"] == "https://plain.example/new"
    assert guide["registration_prefilled"] is False
    assert guide["pat_page_url"] is None


def test_build_guide_without_provider_row_still_lists_values() -> None:
    """未命中服务方行：无 URL 可开（prefilled=False），但值面照样齐（用户可手填）。"""
    provider, row = guidance.match_provider(_shipped_providers(), hosts=["unknown.example"])

    assert provider is None
    guide = guidance.build_guide(
        provider=provider,
        guide=row,
        callback_url=_CALLBACK,
        homepage_url="http://127.0.0.1:9100",
        scopes=[],
        action=flow.GUIDANCE_ACTION_REGISTER_APP,
        access_token_key="MCP_OAUTH_UNKNOWN_EXAMPLE_ACCESS_TOKEN",
    )

    assert guide["registration_url"] is None
    assert guide["pat_page_url"] is None
    assert guide["registration_prefilled"] is False
    assert guide["pat_page_prefilled"] is False
    assert guide["app_name"] == guidance.DEFAULT_APP_NAME
    assert guide["homepage_url"] == "http://127.0.0.1:9100"
    assert guide["callback_url"] == _CALLBACK
    assert guide["scopes"] == []
