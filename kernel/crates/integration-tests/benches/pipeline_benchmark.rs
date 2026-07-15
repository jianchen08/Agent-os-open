//! Task-12 管道引擎性能基准测试
//!
//! 使用 criterion 对管道引擎核心操作进行基准测试：
//! 1. SQLite 四表初始化耗时
//! 2. 单步骤执行耗时（start_run → execute_step → end_run）
//! 3. 多步骤迭代耗时（模拟管道单轮迭代）
//! 4. 状态重放（Patch replay）耗时
//! 5. YAML 配置解析耗时
//!
//! 对应 AC-11-4（traces_to: AC-6）
//! 基准：Rust 版管道单轮迭代耗时 ≤ 0.1 Python 版

use criterion::{criterion_group, criterion_main, BenchmarkId, Criterion};
use std::collections::HashMap;
use std::sync::Arc;

use lingxi_config::ConfigLoader;
use lingxi_core::traits::AdrEngine;
use lingxi_core::types::CompositeStep;
use lingxi_engine::{AdrEngineImpl, SqliteStore};
use lingxi_integration_tests::NoopInvoker;
use serde_json::json;
use tokio::runtime::Runtime;

fn bench_sqlite_init(c: &mut Criterion) {
    c.bench_function("sqlite_four_table_init", |b| {
        b.iter(|| {
            SqliteStore::open_memory().unwrap();
        });
    });
}

fn bench_single_step_execution(c: &mut Criterion) {
    let rt = Runtime::new().unwrap();

    c.bench_function("single_step_execute", |b| {
        b.to_async(&rt).iter(|| async {
            let store = Arc::new(SqliteStore::open_memory().unwrap());
            let invoker = Arc::new(NoopInvoker);
            let engine = AdrEngineImpl::new(store, invoker, "bench_tenant");

            let config = json!({ "pipeline": "bench" });
            let run_id = engine.start_run(&config).await.unwrap();

            let step = CompositeStep {
                name: "bench_step".to_string(),
                plugin: "bench_plugin".to_string(),
                inputs: json!({}),
                outputs: HashMap::new(),
            };
            engine.execute_step(&run_id, &step).await.unwrap();
            engine.end_run(&run_id).await.unwrap();
        });
    });
}

fn bench_multi_step_iteration(c: &mut Criterion) {
    let rt = Runtime::new().unwrap();

    let mut group = c.benchmark_group("pipeline_iteration");
    for num_steps in [3, 5, 10, 20].iter() {
        group.bench_with_input(
            BenchmarkId::from_parameter(num_steps),
            num_steps,
            |b, &n| {
                b.to_async(&rt).iter(|| async {
                    let store = Arc::new(SqliteStore::open_memory().unwrap());
                    let invoker = Arc::new(NoopInvoker);
                    let engine = AdrEngineImpl::new(store, invoker, "bench_tenant");

                    let config = json!({ "pipeline": "bench" });
                    let run_id = engine.start_run(&config).await.unwrap();

                    for i in 0..n {
                        let step = CompositeStep {
                            name: format!("step_{}", i),
                            plugin: format!("plugin_{}", i),
                            inputs: json!({}),
                            outputs: HashMap::new(),
                        };
                        engine.execute_step(&run_id, &step).await.unwrap();
                    }

                    engine.end_run(&run_id).await.unwrap();
                });
            },
        );
    }
    group.finish();
}

fn bench_yaml_parsing(c: &mut Criterion) {
    let yaml_content = r#"
id: bench_agent
name: Benchmark Agent
version: "1.0"
pipeline:
  input:
    - plugin: context_build
      inputs:
        max_history: 20
    - plugin: prompt_build
      inputs:
        system_prompt: "You are an assistant."
  core:
    - plugin: llm_call
      inputs:
        model: gpt-4
        max_tokens: 4096
        temperature: 0.7
  output:
    - plugin: route_arbiter
      inputs:
        rules:
          - condition: "has_tool_calls"
            route: next_tool
          - condition: "default"
            route: next_llm
tools:
  - name: search
    description: "Search tool"
    category: search
  - name: file_edit
    description: "File editor"
    category: file
"#;

    c.bench_function("yaml_config_parse", |b| {
        b.iter(|| {
            let loader = ConfigLoader::new("/tmp/nonexistent", None);
            loader.parse_yaml(yaml_content, "bench").unwrap();
        });
    });
}

fn bench_state_replay(c: &mut Criterion) {
    let rt = Runtime::new().unwrap();

    let mut group = c.benchmark_group("state_replay");
    for num_patches in [10, 50, 100].iter() {
        group.bench_with_input(
            BenchmarkId::from_parameter(num_patches),
            num_patches,
            |b, &n| {
                b.to_async(&rt).iter(|| async {
                    let store = Arc::new(SqliteStore::open_memory().unwrap());
                    let invoker = Arc::new(NoopInvoker);
                    let engine = AdrEngineImpl::new(store, invoker, "replay_tenant");

                    let config = json!({ "pipeline": "replay" });
                    let run_id = engine.start_run(&config).await.unwrap();

                    // 先执行 n 个步骤产生 Patch
                    for i in 0..n {
                        let step = CompositeStep {
                            name: format!("step_{}", i),
                            plugin: format!("plugin_{}", i),
                            inputs: json!({}),
                            outputs: HashMap::new(),
                        };
                        engine.execute_step(&run_id, &step).await.unwrap();
                    }

                    // 再执行一步，触发完整状态重放
                    let step = CompositeStep {
                        name: "replay_step".to_string(),
                        plugin: "replay_plugin".to_string(),
                        inputs: json!({}),
                        outputs: HashMap::new(),
                    };
                    engine.execute_step(&run_id, &step).await.unwrap();
                });
            },
        );
    }
    group.finish();
}

fn bench_config_hash(c: &mut Criterion) {
    let config = json!({
        "pipeline": {
            "id": "bench",
            "steps": [
                {"name": "s1", "plugin": "p1"},
                {"name": "s2", "plugin": "p2"},
                {"name": "s3", "plugin": "p3"}
            ]
        }
    });
    let config_bytes = serde_json::to_vec(&config).unwrap();

    c.bench_function("config_hash_sha256", |b| {
        b.iter(|| {
            use sha2::{Digest, Sha256};
            let mut hasher = Sha256::new();
            hasher.update(&config_bytes);
            let _hash_hex = format!("{:x}", hasher.finalize());
        });
    });
}

criterion_group! {
    name = benches;
    config = Criterion::default().sample_size(20);
    targets =
        bench_sqlite_init,
        bench_single_step_execution,
        bench_multi_step_iteration,
        bench_yaml_parsing,
        bench_state_replay,
        bench_config_hash,
}
criterion_main!(benches);
