"""服务方引导页（创建 OAuth App / 创建 PAT）的匹配与 URL 构造——数据驱动，行来自配置面。

行来源 = 插件配置 ``guidance``（``plugin.json`` 的 ``config_files`` id=``guidance``，
真值文件在 ``config/plugins/oauth_broker/guidance.yaml``）——新增一个服务 = 加一行配置，
不在函数里写死任何服务 URL（URL 全在配置面）。

预填是 **best-effort [未验证]**：query 参数形态（如 GitHub 的
``oauth_application[name]``）为历史形态，本机无法在登录态验证，实现为「带上就打开，
不保证生效」；因此返回值无条件逐条列出用户要手填的值（应用名 / 主页 URL / 回调地址 /
所需 scope），即使预填失效，用户也只是复制粘贴。

我们不代替用户点击、不抓取注册结果（不碰用户的浏览器会话与账号）——注册与同意必须
由人来点，这是 OAuth 的设计本意。
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

DEFAULT_APP_NAME = "Lingxi AgentOS"
#: 参数值占位符（配置面写 ``{callback_url}`` 等，构造时替换）
PLACEHOLDERS = ("app_name", "homepage_url", "callback_url", "scopes", "scopes_comma", "description")


@dataclass(frozen=True)
class ProviderGuide:
    """单个服务方的引导行（URL 可缺省：缺了就只列值不打开页面）。"""

    registration_url: str | None = None
    registration_params: dict[str, str] = field(default_factory=dict)
    token_url: str | None = None
    token_params: dict[str, str] = field(default_factory=dict)
    app_name: str = DEFAULT_APP_NAME
    homepage_url: str | None = None


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _params(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items() if isinstance(value, (str, int, float))}


def load_providers(config: Any) -> dict[str, ProviderGuide]:
    """配置行 → ``{host: ProviderGuide}``（坏行忽略；键统一小写）。

    ``config`` 为 ``plugin.get_config()["guidance"]`` 形态：``{"providers": {host: {...}}}``
    或直接 ``{host: {...}}``（两种形态都认，配置手写容错）。
    """
    rows = config.get("providers") if isinstance(config, dict) and isinstance(config.get("providers"), dict) else config
    if not isinstance(rows, dict):
        return {}
    providers: dict[str, ProviderGuide] = {}
    for host, row in rows.items():
        if not isinstance(row, dict):
            continue
        key = str(host).strip().lower()
        if not key:
            continue
        providers[key] = ProviderGuide(
            registration_url=_text(row.get("registration_url")),
            registration_params=_params(row.get("registration_params")),
            token_url=_text(row.get("token_url")),
            token_params=_params(row.get("token_params")),
            app_name=_text(row.get("app_name")) or DEFAULT_APP_NAME,
            homepage_url=_text(row.get("homepage_url")),
        )
    return providers


def match_provider(
    providers: dict[str, ProviderGuide], *, hosts: Iterable[str | None]
) -> tuple[str | None, ProviderGuide]:
    """按 host 精确匹配（issuer 或服务器 host 任一命中即可）；未命中返回 ``(None, 空行)``。"""
    for host in hosts:
        if not host:
            continue
        guide = providers.get(str(host).strip().lower())
        if guide is not None:
            return str(host).strip().lower(), guide
    return None, ProviderGuide()


def _substitute(params: dict[str, str], values: dict[str, str]) -> dict[str, str]:
    substituted: dict[str, str] = {}
    for key, raw in params.items():
        value = raw
        for name in PLACEHOLDERS:
            value = value.replace("{" + name + "}", values.get(name, ""))
        substituted[key] = value
    return substituted


def _with_params(url: str | None, params: dict[str, str]) -> tuple[str | None, bool]:
    """URL + query 预填；返回 ``(url, 是否带上了预填参数)``。"""
    if not url:
        return None, False
    if not params:
        return url, False
    separator = "&" if urllib.parse.urlsplit(url).query else "?"
    return f"{url}{separator}{urllib.parse.urlencode(params)}", True


def host_of(url: str | None) -> str | None:
    if not url:
        return None
    host = urllib.parse.urlsplit(url).hostname
    return host or None


def build_guide(
    *,
    provider: str | None,
    guide: ProviderGuide,
    callback_url: str,
    homepage_url: str,
    scopes: list[str],
    action: str,
    access_token_key: str,
) -> dict[str, Any]:
    """构造引导返回面（注册页与 PAT 页两个 URL 都给，``action`` 决定本次打开哪个）。"""
    values = {
        "app_name": guide.app_name,
        "homepage_url": guide.homepage_url or homepage_url,
        "callback_url": callback_url,
        "scopes": " ".join(scopes),
        "scopes_comma": ",".join(scopes),
        "description": f"Lingxi AgentOS MCP client access ({callback_url})",
    }
    registration_url, registration_prefilled = _with_params(
        guide.registration_url, _substitute(guide.registration_params, values)
    )
    pat_page_url, pat_page_prefilled = _with_params(guide.token_url, _substitute(guide.token_params, values))
    return {
        "provider": provider,
        "app_name": guide.app_name,
        "homepage_url": values["homepage_url"],
        "callback_url": callback_url,
        "scopes": list(scopes),
        "registration_url": registration_url,
        "registration_prefilled": registration_prefilled,
        "pat_page_url": pat_page_url,
        "pat_page_prefilled": pat_page_prefilled,
        "pat_env_key": access_token_key,
        "auth_config_snippet": '{"type":"bearer","value":"${' + access_token_key + '}"}',
        "opened_action": action,
    }
