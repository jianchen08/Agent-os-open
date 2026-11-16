# @feature: FP-GATE OpenAPI生成器+漂移闸(整改§6) | @ci: python-coverage
"""gen_openapi.py 单测（整改方案 §6 企业级补课第 1 项）。

行为契约（断输入→输出，不钉实现）：
- 生成绿：伪源（server.rs/http_dispatcher.rs 形态片段）→ 合法 OpenAPI 3.1
  文档——路径/方法/路径参数/security 与 P-2 分类一致，/ext 动态面不收录，
  测试面路由不入枚举；
- 漂移检测红绿：生成物被手改 / 改路由忘了再生成 → --check exit 1；
  重新生成后 exit 0；
- fail-closed：源缺失 / AUTH_* 常量抽不到 / 路由面条数低于对账性质断言 /
  路径未落入鉴权面任何一侧 / 清单条目过期 / 受保护前缀悬空 / 同路径重复
  注册 / 路由表达式不可解析 → 同样 exit 1（闸不允许退化成永绿假闸）；
- 性质断言：生成幂等（两次逐字节相同）；每个操作 security ∈ {[], BearerAuth}；
- 负控制（防恒绿）：真实仓状态检查器子进程实跑 --check 退出 0；
  红路径已由上方合成漂移用例钉死。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.unit

# 伪源模板：贴近真实声明形态（常量组 + 多行 .route 调用 + 链式方法 +
# 全限定 handler + 测试面干扰样本）。
SERVER_FAKE = """\
pub const AUTH_PROTECTED_PREFIXES: &[&str] = &[
    "/api/v1/",
    "/uploads/",
    "/ws/",
];

pub const AUTH_PROTECTED_EXACT: &[&str] = &["/metrics"];

pub const AUTH_ANONYMOUS_ALLOW: &[&str] = &[
    "/api/v1/auth/login",
    "/api/v1/auth/me",
    "/ws/chat",
];

pub const AUTH_ANY_USER_ALLOW: &[&str] = &["/api/v1/ws-ticket"];

pub const AUTH_OUTSIDE_MIDDLEWARE: &[&str] = &["/health", "/ext/{*rest}"];

pub fn build_router(state: AppState) -> Router {
    let static_router = Router::new()
        .route("/health", get(health_handler))
        .route("/uploads/{filename}", get(serve_upload_handler))
        .route("/api/v1/schema", get(schema_handler))
        .route(
            "/api/v1/pipelines/{pipeline_id}/pending-inputs",
            get(pending_inputs_list_handler).delete(pending_inputs_clear_handler),
        )
        .route("/api/v1/auth/login", post(login_handler))
        .route("/api/v1/auth/me", get(me_handler))
        .route("/ws/chat", get(ws_handler))
        .route(
            "/api/v1/ws-ticket",
            post(crate::ws_ticket::issue_ticket_handler),
        )
        .route("/metrics", get(metrics_prometheus_handler));
    let router = crate::http_dispatcher::build_router_with_http_routes(
        state.clone(),
        static_router,
    );
    router.with_state(state)
}

#[cfg(test)]
mod tests {
    // 测试面构造的 Router 不是注册面：此路由不得入枚举
    fn t() -> Router {
        Router::new().route("/test-only/ghost", get(ghost_handler))
    }
}
"""

DISPATCHER_FAKE = """\
pub fn build_router_with_http_routes(
    state: crate::routes::AppState,
    static_router: axum::Router<crate::routes::AppState>,
) -> axum::Router<crate::routes::AppState> {
    let mut router = static_router;
    router = router.route("/ext/{*rest}", wildcard_handler);
    router = router.route("/api/v1/datasource/{*rest}", datasource_handler);
    router
}
"""

# 伪源的期望路径全集（/ext 排除；{*rest} 翻译为 {rest}）
FAKE_PATHS = {
    "/health",
    "/uploads/{filename}",
    "/api/v1/schema",
    "/api/v1/pipelines/{pipeline_id}/pending-inputs",
    "/api/v1/auth/login",
    "/api/v1/auth/me",
    "/ws/chat",
    "/api/v1/ws-ticket",
    "/metrics",
    "/api/v1/datasource/{rest}",
}

# 伪源期望 security 分类（空 [] = 匿名豁免/域外；Bearer = 受保护/任意用户）
FAKE_BEARER_PATHS = {
    "/uploads/{filename}",
    "/api/v1/schema",
    "/api/v1/pipelines/{pipeline_id}/pending-inputs",
    "/api/v1/ws-ticket",
    "/metrics",
    "/api/v1/datasource/{rest}",
}


def _load():
    spec = importlib.util.spec_from_file_location("gen_openapi", SCRIPTS / "gen_openapi.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def _gen(server: str = SERVER_FAKE, dispatcher: str = DISPATCHER_FAKE, **kwargs) -> str:
    return mod.generate(server, dispatcher, **kwargs)


def _write_sources(tmp_path: Path, server: str = SERVER_FAKE, dispatcher: str = DISPATCHER_FAKE):
    server_rs = tmp_path / "server.rs"
    dispatcher_rs = tmp_path / "http_dispatcher.rs"
    server_rs.write_text(server, encoding="utf-8")
    dispatcher_rs.write_text(dispatcher, encoding="utf-8")
    return server_rs, dispatcher_rs, tmp_path / "openapi.yaml"


# ── 生成绿 ──────────────────────────────────────────────────────────
def test_generation_contract_green() -> None:
    doc = yaml.safe_load(_gen(min_routes=8))
    assert doc["openapi"] == "3.1.0"
    assert set(doc["paths"]) == FAKE_PATHS  # /ext 不收录 + 测试面不入枚举
    assert "BearerAuth" in doc["components"]["securitySchemes"]
    # security 分类：逐路径核对（P-2 分类镜像）
    for p, item in doc["paths"].items():
        for op in item.values():
            expect = [{"BearerAuth": []}] if p in FAKE_BEARER_PATHS else []
            assert op["security"] == expect, (p, expect)
    # 方法抽取：链式方法路由器两方法都在；操作序固定
    pending = doc["paths"]["/api/v1/pipelines/{pipeline_id}/pending-inputs"]
    assert list(pending) == ["get", "delete"]
    # 路径参数：{name} 提取 + required；{*rest} 翻译为单参数
    params = pending["get"]["parameters"]
    assert [(x["name"], x["in"], x["required"]) for x in params] == [("pipeline_id", "path", True)]
    ds_params = doc["paths"]["/api/v1/datasource/{rest}"]["get"]["parameters"]
    assert ds_params[0]["name"] == "rest"
    assert "尾段通配" in ds_params[0]["description"]
    # any() 透传面：五方法 + 标记；operationId 加方法后缀防撞
    ds = doc["paths"]["/api/v1/datasource/{rest}"]
    assert set(ds) == {"get", "post", "put", "patch", "delete"}
    assert all(op["x-any-method"] is True for op in ds.values())
    assert ds["post"]["operationId"] == "datasource_handler_post"
    # handler 全限定名可溯源
    assert doc["paths"]["/api/v1/ws-ticket"]["post"]["x-handler"] == ("crate::ws_ticket::issue_ticket_handler")
    # 响应骨架：响应键必须是字符串（OpenAPI 契约，YAML 裸数字键是 int）
    for item in doc["paths"].values():
        for op in item.values():
            for code in op["responses"]:
                assert isinstance(code, str), (op["operationId"], code)
    # 骨架按方法/分类差异化（性质：GET 无 422，post/put/patch 有）
    assert "422" not in pending["get"]["responses"]
    assert "422" in doc["paths"]["/api/v1/ws-ticket"]["post"]["responses"]
    # WS 升级端点：101 + 票据 query 参数
    ws = doc["paths"]["/ws/chat"]["get"]
    assert "101" in ws["responses"]
    assert {x["name"] for x in ws["parameters"]} == {"ticket", "token"}


def test_generation_idempotent_and_deterministic() -> None:
    """性质断言：同输入两次生成逐字节相同（漂移闸逐字节比对的前提）。"""
    assert _gen(min_routes=8) == _gen(min_routes=8)


def test_auth_lists_parsed_from_source() -> None:
    """AUTH_* 清单取自 server.rs 单一真值源（非脚本内自建第二套）。"""
    lists = mod.parse_auth_lists(SERVER_FAKE)
    assert lists["AUTH_ANONYMOUS_ALLOW"] == ["/api/v1/auth/login", "/api/v1/auth/me", "/ws/chat"]
    assert lists["AUTH_OUTSIDE_MIDDLEWARE"] == ["/health", "/ext/{*rest}"]


# ── 漂移检测红绿 ────────────────────────────────────────────────────
def test_drift_check_red_then_green(tmp_path: Path) -> None:
    """生成物被手改 → --check 红；重新生成 → 绿（run_gates 消费同一语义）。"""
    server_rs, dispatcher_rs, out = _write_sources(tmp_path)
    assert mod.run_generate(server_rs, dispatcher_rs, out, min_routes=8) == 0
    stale = out.read_text(encoding="utf-8") + "# 手编漂移\n"
    out.write_text(stale, encoding="utf-8")
    assert mod.run_check(server_rs, dispatcher_rs, out, min_routes=8) == 1
    assert mod.run_generate(server_rs, dispatcher_rs, out, min_routes=8) == 0
    assert mod.run_check(server_rs, dispatcher_rs, out, min_routes=8) == 0


def test_route_changed_without_regen_red(tmp_path: Path) -> None:
    """改路由忘了再生成即红（漂移闸的立身之本）。"""
    server_rs, dispatcher_rs, out = _write_sources(tmp_path)
    assert mod.run_generate(server_rs, dispatcher_rs, out, min_routes=8) == 0
    mutated = SERVER_FAKE.replace(
        '.route("/api/v1/schema", get(schema_handler))',
        '.route("/api/v1/schema-v2", get(schema_handler))',
    )
    server_rs.write_text(mutated, encoding="utf-8")
    assert mod.run_check(server_rs, dispatcher_rs, out, min_routes=8) == 1


def test_missing_output_file_red(tmp_path: Path) -> None:
    server_rs, dispatcher_rs, out = _write_sources(tmp_path)
    assert mod.run_check(server_rs, dispatcher_rs, out, min_routes=8) == 1  # 库内无生成物


# ── fail-closed（解析退化/对账不过即红，防永绿假闸）─────────────────
def test_missing_source_file_fail_closed(tmp_path: Path) -> None:
    _, dispatcher_rs, out = _write_sources(tmp_path)
    assert mod.run_check(tmp_path / "ghost.rs", dispatcher_rs, out, min_routes=8) == 1


def test_auth_const_missing_fail_closed() -> None:
    broken = SERVER_FAKE.replace('pub const AUTH_ANY_USER_ALLOW: &[&str] = &["/api/v1/ws-ticket"];', "")
    with pytest.raises(mod.DriftGateError, match="AUTH_ANY_USER_ALLOW"):
        mod.generate(broken, DISPATCHER_FAKE, min_routes=8)


def test_route_count_below_property_assertion_fail_closed() -> None:
    """注册面条数低于对账性质断言（默认 40）即红——扫描器退化防假绿。"""
    with pytest.raises(mod.DriftGateError, match="扫描器大概率失效"):
        mod.generate(SERVER_FAKE, DISPATCHER_FAKE)  # 伪源 10 条 < 40


@pytest.mark.parametrize(
    "mutate",
    [
        # 前缀外新路由未登记 → 未落入鉴权面任何一侧
        ("insert-route",),
        # 豁免清单条目不在注册面 → 过期条目
        ("expired-entry",),
        # 受保护前缀未覆盖任何注册路由 → 悬空前缀
        ("dangling-prefix",),
        # 同路径重复注册 → openapi 面歧义
        ("duplicate-route",),
        # 路由表达式不可解析（非 token handler）→ 解析退化
        ("unparsable-expr",),
    ],
)
def test_reconciliation_fail_closed(mutate: tuple[str]) -> None:
    kind = mutate[0]
    server = SERVER_FAKE
    if kind == "insert-route":
        server = server.replace(
            '.route("/metrics", get(metrics_prometheus_handler));',
            '.route("/metrics", get(metrics_prometheus_handler));\n        .route("/v2/brand-new", get(new_handler));',
        )
        expect = "未落入鉴权面"
    elif kind == "expired-entry":
        server = server.replace(
            '"/ws/chat",\n];',
            '"/ws/chat",\n    "/api/v1/ghost",\n];',
        )
        expect = "不在注册面"
    elif kind == "dangling-prefix":
        server = server.replace('"/ws/",\n];', '"/ws/",\n    "/internal/",\n];')
        expect = "悬空前缀"
    elif kind == "duplicate-route":
        server = server.replace(
            '.route("/metrics", get(metrics_prometheus_handler));',
            '.route("/metrics", get(metrics_prometheus_handler));\n'
            '        .route("/metrics", get(other_metrics_handler));',
        )
        expect = "重复注册"
    else:
        server = server.replace(
            '.route("/api/v1/schema", get(schema_handler))',
            '.route("/api/v1/schema", build_schema_router(secret_state()))',
        )
        expect = "不可解析"
    with pytest.raises(mod.DriftGateError, match=expect):
        mod.generate(server, DISPATCHER_FAKE, min_routes=8)


# ── 负控制（防恒绿）：真实仓活体 ────────────────────────────────────
def test_real_repo_check_green() -> None:
    """真实仓子进程实跑 --check：库内 openapi.yaml 与源码注册面一致退出 0。"""
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "gen_openapi.py"), "--check"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
