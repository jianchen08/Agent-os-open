// @feature: FP-0.2.一 插件协议 | @ci: rust-test
//! D9 管道版本钉住（批 H，2026-09-28）测试面。
//!
//! 覆盖：双键版本缓存命中/未命中、PUT 热更不删旧版本（已钉实例恒取出生
//! 版本）、实例钉住跨轮次稳定、重启恢复两态（hash 一致复用 / 不一致
//! fail-closed）、默认会话（无显式配置）热重载不受影响（回归护栏），
//! 以及 process_via_engine 全链路出生即钉（真实 SqliteStore 持久化 + 冷恢复）。
//! 设计依据：docs/working/模式包工作模式设计_20260928.md D9。

use super::*;

/// 构造临时 config 根并写入指定管道配置（body id 承载版本差异）。
/// `plugin_item` 空 = 纯结构管道（无插件引用，单元测试面，AppState::new()
/// 零插件清单即可编译）；非空 = 可执行项（e2e 轮次需要真实可跑的循环体）。
fn write_pin_config(
    root: &std::path::Path,
    name: &str,
    body_id: &str,
    plugin_item: &str,
) -> std::path::PathBuf {
    let llm_items = if plugin_item.is_empty() {
        "steps: []".to_string()
    } else {
        format!("steps:\n          - {plugin_item}")
    };
    let cfg = root.join("config").join("pipelines");
    std::fs::create_dir_all(&cfg).unwrap();
    std::fs::write(
        cfg.join(format!("{name}.yaml")),
        format!(
            "name: {name}\nloop_bodies:\n  - id: {body_id}\n    steps:\n      - id: llm\n        {llm_items}\n"
        ),
    )
    .unwrap();
    root.join("config")
}

/// 独立目录夹具：唯一配置名 + 临时根 + AppState（返回 user-root guard，
/// 调用方保活到用例结束——drop 即还原全局用户根）。
fn pin_fixture(
    tag: &str,
    body_id: &str,
) -> (
    String,
    std::path::PathBuf,
    AppState,
    crate::test_env::UserSpaceGuard,
) {
    let name = format!("pin_{tag}_{}", uuid::Uuid::new_v4().simple());
    let root = std::env::temp_dir().join(format!("pin_{tag}_{}", uuid::Uuid::new_v4().simple()));
    let guard = crate::test_env::pin_user_root(&root);
    let config_root = write_pin_config(&root, &name, body_id, "");
    (name, config_root, AppState::new(), guard)
}

/// 双键缓存 + PUT 新语义：未钉取用按 latest（命中零重编译、PUT 失效后重编译
/// 新内容）；已钉取用恒按 (name, config_hash) 命中——PUT 失效**不删旧版本
/// 产物**，同一 Arc 直取（D9：热更只对之后出生的实例生效）。
#[tokio::test]
async fn versioned_cache_put_keeps_old_version_for_pinned() {
    let (name, config_root, state, _guard) = pin_fixture("vc", "body_v1");
    let root = config_root.parent().unwrap().to_path_buf();

    // 未钉出生轮：盘面加载 + 编译 + 入集（latest = v1）
    let v1 = crate::server::compiled_for(&state, &config_root, Some(&name), None)
        .await
        .expect("未钉首次取用应成功");
    assert_eq!(v1.bodies[0].id, "body_v1");

    // PUT 热更：盘面改 v2 + 失效（唯一失效路径）
    write_pin_config(&root, &name, "body_v2", "");
    crate::server::pipeline_cache_invalidate(&name);

    // 未钉新实例：重编译盘面 v2（PUT 后出生的实例拿新版）
    let v2 = crate::server::compiled_for(&state, &config_root, Some(&name), None)
        .await
        .expect("失效后未钉取用应重编译新内容");
    assert_eq!(v2.bodies[0].id, "body_v2", "PUT 后未钉取用必须读盘面新内容");

    // 已钉实例（出生在 PUT 之前，钉住 v1 hash）：旧版本产物保留，同 Arc 直取
    let pinned = crate::server::compiled_for(
        &state,
        &config_root,
        Some(&name),
        Some(v1.config_hash.as_str()),
    )
    .await
    .expect("已钉取用应命中保留的旧版本");
    assert!(
        std::sync::Arc::ptr_eq(&v1, &pinned),
        "PUT 不删旧版本条目：已钉实例取回同一编译产物"
    );
    assert_eq!(pinned.bodies[0].id, "body_v1");
}

/// 实例钉住跨轮次稳定：盘面直改（不经 PUT）与 PUT 失效两轮扰动下，钉住
/// 取用恒命中缓存内出生版本（不重读盘面）。
#[tokio::test]
async fn pinned_instance_stable_across_rounds() {
    let (name, config_root, state, _guard) = pin_fixture("st", "body_v1");
    let root = config_root.parent().unwrap().to_path_buf();

    // 出生轮（未钉）：产物 A 入集
    let born = crate::server::compiled_for(&state, &config_root, Some(&name), None)
        .await
        .expect("出生轮应成功");
    let pin_hash = born.config_hash.clone();

    // 轮次 2：盘面直改（不经 PUT，进程内缓存钉住既有契约）
    write_pin_config(&root, &name, "body_v2", "");
    let r2 =
        crate::server::compiled_for(&state, &config_root, Some(&name), Some(pin_hash.as_str()))
            .await
            .expect("钉住取用应成功");
    assert!(std::sync::Arc::ptr_eq(&born, &r2), "盘面直改不影响钉住实例");

    // 轮次 3：PUT 失效后仍取出生版本（旧产物保留）
    crate::server::pipeline_cache_invalidate(&name);
    let r3 =
        crate::server::compiled_for(&state, &config_root, Some(&name), Some(pin_hash.as_str()))
            .await
            .expect("PUT 后钉住取用应成功");
    assert!(
        std::sync::Arc::ptr_eq(&born, &r3),
        "PUT 失效不删旧版本：钉住实例跨轮恒用出生产物"
    );
}

/// 重启恢复（缓存空态）·hash 一致：钉住 hash 与盘面重编译 hash 相同 → 复用
/// （正常恢复路径），产物入集后二次取用同 Arc。
/// "重启"以未预热缓存的唯一配置名模拟（进程级缓存对新名即空集）。
#[tokio::test]
async fn pinned_recovery_reuses_when_hash_matches() {
    let (name, config_root, state, _guard) = pin_fixture("rc", "body_v1");

    // 盘面直接编译取 hash（不经 compiled_for，缓存保持空 = 重启后状态）
    let empty_ids = std::collections::HashSet::new();
    let disk_compiled = crate::server::load_and_compile_by_name(&config_root, &name, &empty_ids)
        .expect("盘面内容应可编译");
    let disk_hash = disk_compiled.config_hash;

    let recovered =
        crate::server::compiled_for(&state, &config_root, Some(&name), Some(disk_hash.as_str()))
            .await
            .expect("hash 一致的重启恢复应复用");
    assert_eq!(recovered.config_hash, disk_hash);
    assert_eq!(recovered.bodies[0].id, "body_v1");

    // 恢复后产物已入集：再取同版本零重编译（同 Arc）
    let again =
        crate::server::compiled_for(&state, &config_root, Some(&name), Some(disk_hash.as_str()))
            .await
            .expect("恢复后取用应命中");
    assert!(std::sync::Arc::ptr_eq(&recovered, &again));
}

/// 重启恢复·hash 不一致：盘面已被修改 → fail-closed 报错（含管道名、期望
/// hash、当前盘面 hash 与新开会话指引），绝不静默换版本；文件被删同族报错。
#[tokio::test]
async fn pinned_recovery_fail_closed_when_disk_changed() {
    let (name, config_root, state, _guard) = pin_fixture("fc", "body_v1");
    let root = config_root.parent().unwrap().to_path_buf();
    let empty_ids = std::collections::HashSet::new();

    // 出生版本 hash（v1 内容）与当前盘面 hash（v2 内容）分属两代
    let born = crate::server::load_and_compile_by_name(&config_root, &name, &empty_ids).unwrap();
    write_pin_config(&root, &name, "body_v2", "");
    let changed = crate::server::load_and_compile_by_name(&config_root, &name, &empty_ids).unwrap();
    assert_ne!(born.config_hash, changed.config_hash, "两代内容 hash 必异");

    // 钉住 v1 hash × 盘面 v2 → fail-closed（性质：错误同时含名/期望/当前 hash）
    let err = crate::server::compiled_for(
        &state,
        &config_root,
        Some(&name),
        Some(born.config_hash.as_str()),
    )
    .await
    .expect_err("盘面已无钉住内容必须报错");
    assert!(err.contains(&name), "错误须带管道名: {err}");
    assert!(
        err.contains(&born.config_hash) && err.contains(&changed.config_hash),
        "错误须同时含期望与当前盘面 hash（区分度双锚）: {err}"
    );
    assert!(err.contains("新开会话"), "错误须含恢复指引: {err}");

    // 同族：配置文件被删 → 同样 fail-closed（带名与期望 hash）
    std::fs::remove_file(config_root.join("pipelines").join(format!("{name}.yaml"))).unwrap();
    let err = crate::server::compiled_for(
        &state,
        &config_root,
        Some(&name),
        Some(changed.config_hash.as_str()),
    )
    .await
    .expect_err("配置被删必须报错");
    assert!(
        err.contains(&name) && err.contains(&changed.config_hash),
        "错误带名与期望 hash: {err}"
    );
}

/// 默认会话回归护栏：无显式配置（None）= autonomous 热重载单例，无视钉住
/// hash 入参（缺省会话不参与钉住，D9 边界）。
#[tokio::test]
async fn none_config_returns_autonomous_singleton_ignoring_pin() {
    let (name, config_root, state, _guard) = pin_fixture("df", "body_v1");
    // 预热显式配置缓存（防 None 路径误走显式分支的对照锚）
    let _ = crate::server::compiled_for(&state, &config_root, Some(&name), None).await;

    let via_none = crate::server::compiled_for(&state, &config_root, None, Some("deadbeef"))
        .await
        .expect("缺省路径应成功");
    let via_reload = crate::server::maybe_reload_compiled_pipeline(&state, &config_root).await;
    assert!(
        std::sync::Arc::ptr_eq(&via_none, &via_reload),
        "None 必须收敛到既有热重载单例（同一 Arc），钉住入参无效"
    );
}

/// 钉住裁定四态：未显式→不钉；显式未钉→出生轮；显式同名→恒用钉住版本；
/// 显式异名→换绑拒绝（fail-closed）。
#[test]
fn resolve_config_pin_adjudicates_four_cases() {
    assert_eq!(
        resolve_config_pin(None, Some(("roleplay", "h1"))),
        Ok(None),
        "默认会话（无显式配置）不参与钉住"
    );
    assert_eq!(
        resolve_config_pin(Some("roleplay"), None),
        Ok(None),
        "出生轮未钉 → None（调用方取到产物后落钉）"
    );
    assert_eq!(
        resolve_config_pin(Some("roleplay"), Some(("roleplay", "h1"))),
        Ok(Some("h1".to_string())),
        "已钉同名 → 恒用钉住版本"
    );
    let err =
        resolve_config_pin(Some("coding"), Some(("roleplay", "h1"))).expect_err("中途换绑必须拒绝");
    assert!(
        err.contains("roleplay") && err.contains("coding"),
        "换绑拒绝须带两配置名: {err}"
    );
    assert!(err.contains("新开会话"), "拒绝信息须含指引: {err}");
}

/// 钉住记录解析：两键成对且为字符串 → Some；任一缺席/非字符串 → None
/// （未钉语义，缺一不可半解析）。
#[test]
fn pinned_config_from_state_requires_both_string_keys() {
    let full = serde_json::json!({
        PIPELINE_CONFIG_PIN_NAME_KEY: "roleplay",
        PIPELINE_CONFIG_PIN_HASH_KEY: "h1",
    });
    assert_eq!(
        pinned_config_from_state(&full),
        Some(("roleplay".to_string(), "h1".to_string())),
        "成对字符串键完整解析"
    );
    // 有区分度的三个残缺形态：缺 hash / 缺 name / 非字符串 hash
    let no_hash = serde_json::json!({ PIPELINE_CONFIG_PIN_NAME_KEY: "roleplay" });
    assert_eq!(pinned_config_from_state(&no_hash), None);
    let no_name = serde_json::json!({ PIPELINE_CONFIG_PIN_HASH_KEY: "h1" });
    assert_eq!(pinned_config_from_state(&no_name), None);
    let non_str = serde_json::json!({
        PIPELINE_CONFIG_PIN_NAME_KEY: "roleplay",
        PIPELINE_CONFIG_PIN_HASH_KEY: 42,
    });
    assert_eq!(
        pinned_config_from_state(&non_str),
        None,
        "非字符串 hash 不解析"
    );
    assert_eq!(pinned_config_from_state(&serde_json::json!({})), None);
}

/// 全链路（process_via_engine，真实内存 SqliteStore + mock invoker）：
/// 出生即钉落库（两键 + run 记账 hash 同指纹）→ PUT 热更 + 冷恢复
/// （registry 摘除 = 重启后冷路径）后同实例恒用出生版本 → PUT 之后出生的
/// 新实例拿新版。
#[tokio::test]
async fn process_via_engine_pins_birth_version_through_cold_recovery() {
    let (state, _invoker, store, _sqlite, _guard) = super::tests::make_engine_state();
    let root = state
        .project_root
        .clone()
        .expect("make_engine_state 恒设 project_root");
    let name = format!("pinmode_{}", uuid::Uuid::new_v4().simple());
    write_pin_config(&root, &name, "pin_v1", "mock_llm_core");
    let tenant = super::TenantContext::new("tenant_pin", "kernel");
    let pipe = format!("pipe_pin_{}", uuid::Uuid::new_v4().simple());

    // 第一轮：显式配置出生 → 钉住 v1 + run 记账
    let r1 = agentos_tenant::scope(
        tenant.clone(),
        process_via_engine(
            &state,
            "第一轮",
            "agentos",
            &pipe,
            "thread_pin",
            "m1",
            "",
            "",
            None,
            None,
            Some(&name),
            "",
        ),
    )
    .await;
    assert!(!r1.failed, "出生轮应成功执行: {}", r1.content);
    let fields1 = store
        .load_pipeline_state(&pipe, &tenant.tenant_id)
        .await
        .expect("出生轮后 state 应可读");
    assert_eq!(
        fields1
            .get(PIPELINE_CONFIG_PIN_NAME_KEY)
            .and_then(|v| v.as_str()),
        Some(name.as_str()),
        "出生钉住 name 键须落库"
    );
    let pin_hash = fields1
        .get(PIPELINE_CONFIG_PIN_HASH_KEY)
        .and_then(|v| v.as_str())
        .expect("出生钉住 hash 键须落库")
        .to_string();
    let run_hash1 = fields1
        .get("run_config_hash")
        .and_then(|v| v.as_str())
        .expect("run 记账 hash 须落库")
        .to_string();
    assert_eq!(
        run_hash1, pin_hash,
        "run 记账 hash 与钉住 hash 同指纹（同编译产物）"
    );

    // PUT 热更：盘面 v2 + 失效；摘除 registry 热缓存（下一轮走冷恢复 =
    // 重启后从 pipeline_state 表读钉住键的路径）
    write_pin_config(&root, &name, "pin_v2", "mock_llm_core");
    crate::server::pipeline_cache_invalidate(&name);
    agentos_session::pipeline_state_registry::global_registry().remove(&tenant.tenant_id, &pipe);

    // 第二轮：同实例 + 冷恢复 → 恒用出生版本（run hash 不变）
    let r2 = agentos_tenant::scope(
        tenant.clone(),
        process_via_engine(
            &state,
            "第二轮",
            "agentos",
            &pipe,
            "thread_pin",
            "m2",
            "",
            "",
            None,
            None,
            Some(&name),
            "",
        ),
    )
    .await;
    assert!(!r2.failed, "钉住轮应成功执行: {}", r2.content);
    let fields2 = store
        .load_pipeline_state(&pipe, &tenant.tenant_id)
        .await
        .unwrap();
    let run_hash2 = fields2
        .get("run_config_hash")
        .and_then(|v| v.as_str())
        .expect("第二轮 run 记账 hash 须落库");
    assert_eq!(
        run_hash2, run_hash1,
        "PUT 热更 + 冷恢复后已钉实例恒用出生版本（记账不漂移）"
    );

    // 对照：PUT 之后出生的新实例 → 新版本（hash ≠ 出生版本）
    let pipe_new = format!("pipe_pin_{}", uuid::Uuid::new_v4().simple());
    let r3 = agentos_tenant::scope(
        tenant.clone(),
        process_via_engine(
            &state,
            "新实例",
            "agentos",
            &pipe_new,
            "thread_pin",
            "m3",
            "",
            "",
            None,
            None,
            Some(&name),
            "",
        ),
    )
    .await;
    assert!(!r3.failed, "新实例轮应成功执行: {}", r3.content);
    let fields3 = store
        .load_pipeline_state(&pipe_new, &tenant.tenant_id)
        .await
        .unwrap();
    let run_hash3 = fields3
        .get("run_config_hash")
        .and_then(|v| v.as_str())
        .expect("新实例 run 记账 hash 须落库");
    assert_ne!(
        run_hash3, run_hash1,
        "PUT 后出生的新实例必须用新版（热更只对之后出生的实例生效）"
    );
    assert_eq!(
        fields3
            .get(PIPELINE_CONFIG_PIN_HASH_KEY)
            .and_then(|v| v.as_str()),
        Some(run_hash3),
        "新实例钉住的就是新版 hash"
    );
}
