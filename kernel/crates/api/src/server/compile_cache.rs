//! 管道编译缓存与配置指纹（热重载检测 + D9 出生即钉裁定）。

use super::ws_entry::PIPELINE_CONFIG_TTL;
use super::*;
/// 热重载检测的全局缓存：上次检测时刻 + 上次编译指纹（配置 mtime ⊕ manifests
/// 摘要，B6 双维键域）+ 上次编译产物（G10：指纹不变直接复用）。进程级单例，
/// 所有 chat 请求共享，避免反复 stat/解析/编译。
#[derive(Clone)]
struct ConfigReloadState {
    last_check: std::time::Instant,
    last_fingerprint: u64,
    compiled: Option<Arc<agentos_engine::compiler::CompiledPipeline>>,
}

/// 进程级单例缓存：按 config_root 隔离（测试多根并发、生产单根）。
/// 键 = 配置根目录绝对路径——不同 config_root（如各测试的临时目录）互不污染。
static CONFIG_RELOAD_CACHE: OnceLock<ParkingRwLock<HashMap<PathBuf, ConfigReloadState>>> =
    OnceLock::new();

fn config_reload_cache() -> &'static ParkingRwLock<HashMap<PathBuf, ConfigReloadState>> {
    CONFIG_RELOAD_CACHE.get_or_init(|| ParkingRwLock::new(HashMap::new()))
}

/// 计算管道编译缓存指纹：配置面 mtime（autonomous.yaml + config/steps/*.yaml）
/// ⊕ manifests 摘要（B6，编译输入第二维——步骤服务索引与已知插件集合均出自
/// manifests）。配置面用 mtime（stat 微秒级，热路径可接受）；声明面是内存
/// 集合，mtime 无法表达，用按 id 排序的内容序列化 hash——内容不变（纯代码
/// 变更）命中缓存，任何声明变更必失效。任一维变化 → 指纹变化 → 重编译。
pub(super) fn compute_config_fingerprint(
    config_root: &std::path::Path,
    manifests: &[agentos_core::traits::PluginManifest],
) -> u64 {
    use std::collections::hash_map::DefaultHasher;
    use std::hash::Hasher;
    let mut hasher = DefaultHasher::new();

    let mtime_secs = |p: &std::path::Path| -> u64 {
        std::fs::metadata(p)
            .and_then(|m| m.modified())
            .ok()
            .and_then(|t| t.duration_since(SystemTime::UNIX_EPOCH).ok())
            .map(|d| d.as_secs())
            .unwrap_or(0)
    };

    // autonomous.yaml（核心管道配置）——取生效落点（用户空间优先）：用户在管道
    // 配置页保存后指纹必须跟着变，否则热重载永远看不到用户改动。
    let pipeline_yaml =
        crate::pipeline_loader::resolve_pipeline_config_path(config_root, "autonomous.yaml");
    hasher.write(b"autonomous:");
    hasher.write_u64(mtime_secs(&pipeline_yaml));

    // config/steps/ 目录（公共 step 库）
    let steps_dir = config_root.join("steps");
    if let Ok(entries) = std::fs::read_dir(&steps_dir) {
        let mut paths: Vec<_> = entries
            .filter_map(|e| e.ok())
            .filter_map(|e| {
                let p = e.path();
                if p.is_file()
                    && (p.extension().and_then(|x| x.to_str()) == Some("yaml")
                        || p.extension().and_then(|x| x.to_str()) == Some("yml"))
                {
                    Some((p.file_name()?.to_string_lossy().to_string(), mtime_secs(&p)))
                } else {
                    None
                }
            })
            .collect();
        paths.sort_by(|a, b| a.0.cmp(&b.0));
        for (name, mtime) in paths {
            hasher.write(name.as_bytes());
            hasher.write(b":");
            hasher.write_u64(mtime);
        }
    }

    // manifests 摘要（声明维，B6）：按 id 排序消除集合序噪声，逐个序列化哈希。
    let mut sorted: Vec<&agentos_core::traits::PluginManifest> = manifests.iter().collect();
    sorted.sort_by(|a, b| a.id.cmp(&b.id));
    hasher.write(b"manifests:");
    for m in sorted {
        let json = serde_json::to_string(m).expect("manifest 序列化不可失败");
        hasher.write(json.as_bytes());
        hasher.write(b"\n");
    }

    hasher.finish()
}

/// 已知插件 id 的**活集合**：从共享 manifests store 现读（watcher 热发现的
/// 增量合并即时可见），供管道编译的命中规则③与 executor 的 step 解析使用——
/// 新插件热注册后，管道 YAML 引用其 id 即可编译解析，无需重启内核。
pub(crate) async fn live_plugin_ids(state: &AppState) -> std::collections::HashSet<String> {
    state
        .manifests
        .read()
        .await
        .iter()
        .map(|m| m.id.clone())
        .collect()
}

/// Pull 热加载：按需重载并**编译**管道配置（G10 生产路径）。每次
/// `process_via_engine` 执行前调用，返回本次执行应使用的
/// [`CompiledPipeline`]（启动期编译的语义，运行时零解析）；`None` = 尚无一次
/// 成功编译——调用方显式报错拒绝空跑（空管道轮次零步"完成" = 零回复零错误帧
/// 的假成功）。策略（双层短路）：①TTL 门——距上次检测不足 1s 直接返回缓存
/// 产物；②指纹比对——过期后计算指纹（配置 mtime ⊕ manifests 摘要）与缓存
/// 比对，相同 → 缓存产物，不同 → 重载 + 编译（失败保留旧产物 + warn）。快照
/// 语义：在途 run 持调用时的 `Arc` 跑完，新 run 取新产物。自愈：编译对
/// (配置, manifests) 确定性——同指纹必同结果，失败态不占位缓存；修复到达
/// （提供方热注册/能力变更/配置修复）必改指纹，下个 TTL 窗口自动重编译。
pub(crate) async fn maybe_reload_compiled_pipeline(
    state: &AppState,
    config_root: &std::path::Path,
) -> Option<Arc<agentos_engine::compiler::CompiledPipeline>> {
    let now = std::time::Instant::now();
    // TTL 门 + 指纹比对在同一把读锁下快照（按 config_root 取，测试多根并发隔离）
    let (needs_check, cached) = {
        let cache = config_reload_cache().read();
        match cache.get(config_root) {
            None => (true, None),
            Some(c) => (
                now.duration_since(c.last_check) >= PIPELINE_CONFIG_TTL,
                c.compiled.clone(),
            ),
        }
    };
    if !needs_check {
        return cached;
    }

    // 指纹双维（配置 stat ⊕ manifests 快照），仅 TTL 过期路径执行。
    let manifests_snapshot = state.manifests.read().await.clone();
    let new_fp = compute_config_fingerprint(config_root, &manifests_snapshot);
    let (reload, cached_compiled) = {
        let mut cache = config_reload_cache().write();
        let (stale, compiled) = match cache.get(config_root) {
            None => (true, None),
            Some(c) => (c.last_fingerprint != new_fp, c.compiled.clone()),
        };
        // 无论是否重载都刷新检测时刻与指纹基线（修复到达必改指纹，见函数头"自愈"）。
        cache.insert(
            config_root.to_path_buf(),
            ConfigReloadState {
                last_check: now,
                last_fingerprint: new_fp,
                compiled: compiled.clone(),
            },
        );
        (stale, compiled)
    };

    if !reload {
        return cached_compiled;
    }

    // 指纹变化 → 重新加载 + 校验 + 编译
    //（G10：when AST / 引用 / 环在加载期暴露）
    info!("Pipeline config changed on disk, reloading + recompiling...");
    let manifests_snapshot = state.manifests.read().await.clone();
    let recompiled = load_and_compile(config_root, &manifests_snapshot);
    let new_compiled = match recompiled {
        Ok(c) => {
            info!(
                pipeline = %c.name,
                bodies = c.bodies.len(),
                "Pipeline config hot-reloaded and compiled successfully"
            );
            Arc::new(c)
        }
        Err(e) => {
            warn!(error = %e, "Hot reload: pipeline recompile failed, keeping old compiled config");
            // 失败态不入缓存：编译对 (配置, manifests) 确定性——同指纹必同结果；
            // 修复到达必改指纹，下个 TTL 窗口自动重编译（双维同权驱动自愈）。
            return cached_compiled;
        }
    };
    // 编译成功 → 原子更新缓存（后续请求零重编译）。
    // 注意：读锁快照必须先落变量再释放——`if let Some(x) = lock.read().clone()` 的
    // scrutinee 临时值（读锁 guard）生命周期会延长到 if 块结束，块内拿写锁即
    // 同线程死锁（读锁未释放等写锁）。先求值再拿写锁，锁严格分离。
    let cached_snapshot = config_reload_cache().read().get(config_root).cloned();
    if let Some(mut c) = cached_snapshot {
        c.compiled = Some(Arc::clone(&new_compiled));
        config_reload_cache()
            .write()
            .insert(config_root.to_path_buf(), c);
    }
    Some(new_compiled)
}

/// 加载 + 校验 + 编译管道（供热重载与启动期 fail-fast 复用）：任一步失败返回
/// Err（含上下文）——启动期调用方据此 panic 拒绝启动，热重载调用方据此保留旧产物。
/// 步骤服务③级已接线（ADR 2026-10-08-step-service-level3-wiring）：本入口恒构
/// [`build_step_service_index`] 传入 with_hooks 装配——③级命中生产可达（执行死层
/// ①接线分支，ADR 2026-09-09-kernel-dead-layer-adjudication）。兼容映射保证存量
/// 零破坏：纯步骤插件隐式默认注册、显式条目 name==插件 id 映射 method None（与④
/// 直引等价）；hooks 沿传空（无声明需求，hooks 链仍按原裁定保留，yaml hooks 字段
/// 在 `to_internal` 丢弃不变）。
pub fn load_and_compile(
    config_root: &std::path::Path,
    manifests: &[agentos_core::traits::PluginManifest],
) -> Result<agentos_engine::compiler::CompiledPipeline, String> {
    load_and_compile_by_name(config_root, "autonomous", manifests)
}

/// 按名加载 + 校验 + 编译管道（[`load_and_compile`] 的通用形态）。
///
/// 错误信息始终携带管道配置名（调用方透传给请求方定位用了哪个配置）。
pub fn load_and_compile_by_name(
    config_root: &std::path::Path,
    name: &str,
    manifests: &[agentos_core::traits::PluginManifest],
) -> Result<agentos_engine::compiler::CompiledPipeline, String> {
    use agentos_engine::compiler::{build_step_service_index, compile_pipeline_with_checks};
    let plugin_ids: std::collections::HashSet<String> =
        manifests.iter().map(|m| m.id.clone()).collect();
    let index = build_step_service_index(manifests)
        .map_err(|e| format!("步骤服务索引构建失败: {}", e.message))?;
    let (pipeline, checks) = load_pipeline_with_checks(config_root, name)
        .map_err(|e| format!("加载管道配置 '{name}' 失败: {e}"))?;
    let steps = load_step_library(config_root).map_err(|e| format!("加载公共 step 库失败: {e}"))?;
    validate_no_name_conflicts(&pipeline, &steps, &plugin_ids)
        .map_err(|conflict| format!("管道配置 '{name}' 命名冲突: {conflict}"))?;
    // L2 嵌套不变量装载期校验（ADR 2026-11-05-timeout-taxonomy-and-merge §5.2）：
    // 外层声明 ≥ 内层声明，违反拦截（指名哪层包不住哪层）——矛盾在装载时炸
    // 而非运行 600s 后炸；动态面（LLM 工具面）由 L1 让位机制承载，不在此拦。
    crate::timeout_nesting::validate_timeout_nesting(&pipeline, &steps, manifests, &index)
        .map_err(|e| format!("管道配置 '{name}' 超时嵌套校验失败: {e}"))?;
    compile_pipeline_with_checks(
        &pipeline,
        &steps,
        &plugin_ids,
        Some(&index),
        &checks.body_checks,
        &checks.safety_locked_steps,
    )
    .map_err(|e| format!("管道配置 '{name}' 编译失败: {}", e.message))
}

// ═════════════════════════════════════════════════════════════════
// 管道编译缓存（PipelineCache）：显式指定的管道配置按需加载编译
// ═════════════════════════════════════════════════════════════════

/// 单个管道配置名的编译产物版本集（D9 版本钉住，批 H）。`versions` 按
/// config_hash 双键保留全部历史版本产物——PUT 热更不删旧条目（已钉实例按
/// (name, config_hash) 恒取出生版本）；`latest` = 当前盘面最新版的 config_hash，
/// PUT 热更后置 None（下次未钉取用按盘面重编译产新版本入集）。版本集合进程内
/// 只增不删：每次 PUT 至多新增一个版本，产物为配置规模的小结构（无 messages 等
/// 大字段），进程生命周期内不做淘汰——淘汰会让已钉实例被迫走盘面重编译路径，
/// 配置已改时误触 fail-closed（旧产物必须保留）。
pub(crate) struct PipelineVersionSet {
    latest: Option<String>,
    versions: HashMap<String, Arc<agentos_engine::compiler::CompiledPipeline>>,
}

impl PipelineVersionSet {
    pub(crate) fn empty() -> Self {
        Self {
            latest: None,
            versions: HashMap::new(),
        }
    }

    /// 收录一个编译产物；`as_latest` = 同时设为当前盘面最新版。同 hash 已有
    /// 产物时保持原 Arc（身份稳定：盘面改回旧内容后与既有实例共享同产物）。
    pub(crate) fn upsert_version(
        &mut self,
        compiled: Arc<agentos_engine::compiler::CompiledPipeline>,
        as_latest: bool,
    ) -> Arc<agentos_engine::compiler::CompiledPipeline> {
        if as_latest {
            self.latest = Some(compiled.config_hash.clone());
        }
        self.versions
            .entry(compiled.config_hash.clone())
            .or_insert(compiled)
            .clone()
    }
}

static PIPELINE_COMPILED_CACHE: OnceLock<ParkingRwLock<HashMap<String, PipelineVersionSet>>> =
    OnceLock::new();

pub(crate) fn pipeline_compiled_cache(
) -> &'static ParkingRwLock<HashMap<String, PipelineVersionSet>> {
    PIPELINE_COMPILED_CACHE.get_or_init(|| ParkingRwLock::new(HashMap::new()))
}

/// 编译产物缓存驻留快照（堆栈级诊断面 memory-breakdown 消费）：
/// (管道配置数, 版本产物总数)。版本集随配置热更只增（D9 钉住契约要求
/// 保活）——观测项，条目数为实测计数。
pub(crate) fn pipeline_compiled_cache_stats() -> (usize, usize) {
    let cache = pipeline_compiled_cache().read();
    (
        cache.len(),
        cache.values().map(|set| set.versions.len()).sum(),
    )
}

/// 启动期预入编译产物（autonomous 单例；现状行为的缓存面）：入版本集并设为
/// 当前最新版。
pub fn pipeline_cache_seed(name: &str, compiled: Arc<agentos_engine::compiler::CompiledPipeline>) {
    let mut cache = pipeline_compiled_cache().write();
    let set = cache
        .entry(name.to_string())
        .or_insert_with(PipelineVersionSet::empty);
    set.upsert_version(compiled, true);
}

/// PUT 管道配置热更写盘后调用：清该配置的"当前盘面最新版"指针——下次未钉
/// 取用按盘面重编译产新版本入集；**不删任何历史版本产物**（D9：已钉实例继续
/// 按 (name, config_hash) 取出生版本，热更只对之后出生的实例生效）。不存在的
/// 键幂等。
pub(crate) fn pipeline_cache_invalidate(name: &str) {
    if let Some(set) = pipeline_compiled_cache().write().get_mut(name) {
        if set.latest.take().is_some() {
            info!(pipeline_config = %name, "Pipeline latest version invalidated (config updated; historical versions kept)");
        }
    }
}

/// 管道编译产物选择收敛点（CompiledPipeline 唯一运行时取用面）。
///
/// - `config_id = None` → autonomous 单例（[`maybe_reload_compiled_pipeline`]
///   热重载路径）——默认会话不参与版本钉住（D9 = 显式绑定与任务派生管道）；
/// - `Some(id)` 且未钉（`pinned_hash = None`）→ 当前最新版：缓存 latest 命中
///   直接返回，未命中按盘面加载+编译+入集返回（调用方据返回产物的
///   `config_hash` 落钉 = 出生即钉）；
/// - `Some(id)` 且已钉 → 恒取钉住版本：缓存按 (name, hash) 命中直接返回；
///   未命中（进程重启后缓存已空）按盘面重编译并比对 hash——一致复用（正常
///   恢复），不一致 → `Err` fail-closed（含期望/当前盘面 hash，提示新开会话
///   或重派任务，不静默换版本）；
/// - 任一步失败 → `Err`（带管道名）——**不回落 autonomous**：用哪个管道就是
///   哪个，静默回落 = 用错管道。`pinned_hash` 仅对显式配置生效（`None` 缺省
///   路径无视它，由调用方 [`resolve_config_pin`] 保证不出现在缺省轮）。
pub(crate) async fn compiled_for(
    state: &AppState,
    config_root: &std::path::Path,
    config_id: Option<&str>,
    pinned_hash: Option<&str>,
) -> Result<Arc<agentos_engine::compiler::CompiledPipeline>, String> {
    let Some(name) = config_id.filter(|s| !s.is_empty()) else {
        // autonomous 缺省路径：无可用编译产物显式报错拒绝空跑（零步"完成" =
        // 零回复零错误帧的假成功）；Err 经既有失败面可见，自愈靠指纹双维——
        // 修复到达必改指纹，下个 TTL 窗口自动重编译。
        return maybe_reload_compiled_pipeline(state, config_root).await.ok_or_else(|| {
            "管道配置 'autonomous' 暂不可用：编译失败且无历史产物，拒绝空管道执行（修复配置或补齐步骤提供方插件后自动恢复）"
                .to_string()
        });
    };
    if let Some(expected) = pinned_hash {
        return compiled_for_pinned(state, config_root, name, expected).await;
    }
    // 未钉（出生轮）：当前最新版命中零重编译（磁盘直改不经 PUT 不生效的
    // 既有契约不变）。
    let latest_hit = pipeline_compiled_cache().read().get(name).and_then(|set| {
        set.latest
            .as_ref()
            .and_then(|hash| set.versions.get(hash).cloned())
    });
    if let Some(hit) = latest_hit {
        return Ok(hit);
    }
    let compiled = compile_explicit_config(state, config_root, name).await?;
    let mut cache = pipeline_compiled_cache().write();
    let set = cache
        .entry(name.to_string())
        .or_insert_with(PipelineVersionSet::empty);
    Ok(set.upsert_version(compiled, true))
}

/// 钉住取用：恒按 (name, expected) 版本取产物；缓存未命中（重启后）按盘面
/// 重编译比对——hash 一致复用（正常恢复），不一致 fail-closed。
pub(crate) async fn compiled_for_pinned(
    state: &AppState,
    config_root: &std::path::Path,
    name: &str,
    expected: &str,
) -> Result<Arc<agentos_engine::compiler::CompiledPipeline>, String> {
    let pinned_hit = pipeline_compiled_cache()
        .read()
        .get(name)
        .and_then(|set| set.versions.get(expected).cloned());
    if let Some(hit) = pinned_hit {
        return Ok(hit);
    }
    // 重启恢复：缓存无该版本，按盘面重编译比对（与 compiled_for 缺配置
    // "不回落 autonomous、用错管道不如报错"同精神——盘面已无同内容时
    // fail-closed，不静默换版本）。
    let path =
        crate::pipeline_loader::resolve_pipeline_config_path(config_root, &format!("{name}.yaml"));
    if !path.exists() {
        return Err(format!(
            "管道配置 '{name}' 已不存在（{}），实例钉住版本（config_hash={expected}）\
             不可恢复——请新开会话或重派任务",
            path.display()
        ));
    }
    let compiled = compile_explicit_config(state, config_root, name).await?;
    if compiled.config_hash != expected {
        return Err(format!(
            "管道配置 '{name}' 已被修改，钉住版本不可恢复：期望 config_hash={expected}，\
             当前盘面 config_hash={}——请新开会话或重派任务（不静默换版本）",
            compiled.config_hash
        ));
    }
    info!(
        pipeline_config = %name,
        config_hash = %expected,
        "Pinned pipeline version recovered from disk (hash matched)"
    );
    let mut cache = pipeline_compiled_cache().write();
    let set = cache
        .entry(name.to_string())
        .or_insert_with(PipelineVersionSet::empty);
    Ok(set.upsert_version(compiled, true))
}

/// 按名加载 + 编译一个显式配置（未钉/钉住恢复共用底座）。
///
/// 显式指定的配置必须存在：缺文件 = Err（不降级空管道、不回落 autonomous
/// ——静默跑错管道比报错更糟）。缺文件降级仅属 autonomous 启动加载
/// （load_pipeline_config 的缺省启动契约）。
pub(crate) async fn compile_explicit_config(
    state: &AppState,
    config_root: &std::path::Path,
    name: &str,
) -> Result<Arc<agentos_engine::compiler::CompiledPipeline>, String> {
    let path =
        crate::pipeline_loader::resolve_pipeline_config_path(config_root, &format!("{name}.yaml"));
    if !path.exists() {
        return Err(format!("管道配置 '{name}' 不存在（{}）", path.display()));
    }
    let manifests_snapshot = state.manifests.read().await.clone();
    let compiled = Arc::new(load_and_compile_by_name(
        config_root,
        name,
        &manifests_snapshot,
    )?);
    info!(pipeline_config = %name, "Pipeline config lazily loaded and compiled");
    Ok(compiled)
}

// ── D9 版本钉住（实例级）：出生即钉的读写面 ──────────────────────────
//
// 机制选择（免跨层传 hash）：不扩 modeBinding 快照/发送链帧，kernel 侧按
// pipeline_id（管道实例唯一坐标）钉——实例首次解析显式配置时把
// (name, config_hash) 落管道 state 持久键，后续轮次恢复合并回 initial_state
// 后恒按钉住版本取产物。前端/插件零改动。

/// 管道实例版本钉住的 state 持久键（D9 出生即钉，批 H）。点号键，与 task.id
/// 同款约定；两键恒成对写入（出生轮一次），消费面 = stage_execute 逐轮读取。
pub(super) const PIPELINE_CONFIG_PIN_NAME_KEY: &str = "pipeline_config_pin.name";
pub(super) const PIPELINE_CONFIG_PIN_HASH_KEY: &str = "pipeline_config_pin.config_hash";

/// 从恢复后的 initial_state 解析实例钉住记录（热路径 registry 快照 / 冷路径
/// pipeline_state 表，两路恢复均进 state 顶层键）。任一键缺席/非字符串 →
/// None（未钉：出生轮或默认会话）。
pub(super) fn pinned_config_from_state(
    initial_state: &serde_json::Value,
) -> Option<(String, String)> {
    let name = initial_state
        .get(PIPELINE_CONFIG_PIN_NAME_KEY)?
        .as_str()?
        .to_string();
    let hash = initial_state
        .get(PIPELINE_CONFIG_PIN_HASH_KEY)?
        .as_str()?
        .to_string();
    Some((name, hash))
}

/// D9 钉住裁定（出生即钉）：
/// - 显式配置 + 已钉同名 → `Ok(Some(hash))`：恒用钉住版本；
/// - 显式配置 + 未钉 → `Ok(None)`：出生轮，调用方取到产物后落钉；
/// - 显式配置 + 钉住异名 → `Err`：实例出生即钉，中途换绑配置名 = fail-closed
///   （换配置请新开会话/重派任务，与内容层 fail-closed 同精神）；
/// - 无显式配置 → `Ok(None)`：默认会话 autonomous 热重载，不参与钉住。
pub(super) fn resolve_config_pin(
    explicit: Option<&str>,
    pinned: Option<(&str, &str)>,
) -> Result<Option<String>, String> {
    let Some(name) = explicit else {
        return Ok(None);
    };
    match pinned {
        Some((pinned_name, pinned_hash)) if pinned_name == name => {
            Ok(Some(pinned_hash.to_string()))
        }
        Some((pinned_name, _)) => Err(format!(
            "管道实例已钉住配置 '{pinned_name}'，拒绝中途改绑 '{name}'\
             （出生即钉：换配置请新开会话或重派任务）"
        )),
        None => Ok(None),
    }
}
