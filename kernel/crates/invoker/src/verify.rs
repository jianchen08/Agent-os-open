//! # 双写一致性校验（G2）：manifest 声明 vs 实际暴露
//!
//! 工具的 manifest 描述（`capabilities.tools`）与 sidecar 实际上报（MCP
//! `tools/list`）是两份手写/两处产生，必然漂移。本模块提供**对照函数**
//! （纯逻辑，无 IO 无时序，可同步单测）：
//!
//! - [`ActualTool`]：sidecar 实际上报的工具（MCP `tools/list` 项）。
//! - [`VerifyMismatch`]：一类漂移——声明有实际无（missing）/ 实际有声明无
//!   （undeclared）/ 同名工具参数 schema 不一致（schema_mismatch）。
//! - [`compare_tools`]：对照 declared vs actual，产出漂移清单。
//!
//! 处置（§6.4 注册失败分级降级）：安装/升级时强制跑一次（spawn → 校验 →
//! 回收，不破坏懒加载），漂移工具的贡献**拒绝注册** + 启动报告可见，不静默；
//! `POST /api/v1/plugins/validate-all` 全量巡检；lazy 插件首次激活复核（warn）。
//!
//! [来源: docs/working/重要设计/插件三轨一致性与Cordis机制迁移计划.md §G2]

use agentos_core::traits::{PluginManifest, ToolCapability};

/// manifest 的"sidecar 应上报工具全集"= capabilities.tools ∪ services
/// （D.6 槽位拆分：services 条目仍是 sidecar 上的 MCP 工具，只是不进
/// LLM 注册面——双写校验的对照集合必须合并两者，否则迁移后系统插件的
/// 服务方法会全部误报 undeclared）。
/// 2026-08-18：服务条目经 auto-backfill 携带 `input_schema`/`output_schema`
/// （在提供方 plugin.json），此处一并映射进对照集合——G2 据此比对"插件↔服务"
/// 调用形状（声明 vs 提供方实际 tools/list）。
pub fn declared_with_services(manifest: &PluginManifest) -> Vec<ToolCapability> {
    let mut declared = manifest.capabilities.tools.clone();
    declared.extend(
        manifest
            .capabilities
            .services
            .iter()
            .map(|s| ToolCapability {
                name: s.name.clone(),
                description: s.description.clone(),
                input_schema: s.input_schema.clone(),
                output_schema: s.output_schema.clone(),
                category: None,
                ui: None,
                render: None,
                smoke: None,
            }),
    );
    declared
}

/// sidecar 实际上报的工具（MCP `tools/list` 项 `{name, description, inputSchema}`）。
#[derive(Debug, Clone, PartialEq)]
pub struct ActualTool {
    pub name: String,
    pub description: Option<String>,
    /// 参数 schema（JSON Schema 子集）。缺失 = 空对象。
    pub input_schema: serde_json::Value,
}

/// 从 MCP `tools/list` 返回的原始 JSON 解析实际工具列表。
///
/// 结构：`{ "tools": [ { "name": ..., "description": ..., "inputSchema": ... } ] }`。
/// 解析失败（缺 name / tools 非数组）→ 该项跳过并计入 `malformed` 计数——
/// 上报畸形本身就是漂移信号（列表可继续对照，缺项会在 missing 侧暴露）。
pub fn parse_actual_tools(raw: &serde_json::Value) -> (Vec<ActualTool>, usize) {
    let mut tools = Vec::new();
    let mut malformed = 0usize;
    let Some(arr) = raw.get("tools").and_then(|v| v.as_array()) else {
        return (tools, 1);
    };
    for item in arr {
        let Some(name) = item.get("name").and_then(|v| v.as_str()) else {
            malformed += 1;
            continue;
        };
        tools.push(ActualTool {
            name: name.to_string(),
            description: item
                .get("description")
                .and_then(|v| v.as_str())
                .map(|s| s.to_string()),
            input_schema: item
                .get("inputSchema")
                .cloned()
                .unwrap_or(serde_json::json!({})),
        });
    }
    (tools, malformed)
}

/// 一类声明 vs 实际漂移。
#[derive(Debug, Clone, PartialEq)]
pub enum VerifyMismatch {
    /// 声明有、实际没有——实现缺失（拒绝该条贡献）。
    Missing {
        /// 声明的工具名（manifest `capabilities.tools[].name`）。
        name: String,
    },
    /// 实际有、声明没有——未声明暴露（不注册，但记录）。
    Undeclared {
        /// 实际工具名（sidecar 上报）。
        name: String,
    },
    /// 同名工具的输入参数 schema 不一致。
    SchemaMismatch {
        name: String,
        /// manifest 声明的 schema（缺省空对象）。
        declared: serde_json::Value,
        /// sidecar 上报的 schema。
        actual: serde_json::Value,
    },
}

/// 对照 manifest 声明 vs 实际暴露，产出漂移清单（顺序：missing → undeclared →
/// schema_mismatch，均为稳定排序，便于测试与报告稳定）。
///
/// - 名字集合差：声明有实际无 → [`VerifyMismatch::Missing`]；实际有声明无 →
///   [`VerifyMismatch::Undeclared`]。
/// - 同名工具：**manifest 声明了 `input_schema` 才做逐字比较**（serde_json 的
///   Map 比较键序无关）；`None` = 声明未覆盖 schema，以实际为准不比对——
///   存量插件（0.2 早期）是"代码内 schema"模式（manifest 只写 name/description，
///   完整 schema 在插件代码里由 SDK 上报），此时比对必然误报；声明了则严格，
///   漂移即拒绝（新插件按"manifest 为唯一书写处"走严格通道）。
pub fn compare_tools(declared: &[ToolCapability], actual: &[ActualTool]) -> Vec<VerifyMismatch> {
    let mut mismatches = Vec::new();

    // 名字集合差（missing / undeclared）
    let declared_names: std::collections::HashSet<&str> =
        declared.iter().map(|t| t.name.as_str()).collect();
    let actual_names: std::collections::HashSet<&str> =
        actual.iter().map(|t| t.name.as_str()).collect();

    // 声明序保持 stable（missing 按 manifest 声明顺序）
    for tool in declared {
        if !actual_names.contains(tool.name.as_str()) {
            mismatches.push(VerifyMismatch::Missing {
                name: tool.name.clone(),
            });
        }
    }
    // 实际序保持 stable（undeclared 按上报顺序）
    for tool in actual {
        if !declared_names.contains(tool.name.as_str()) {
            mismatches.push(VerifyMismatch::Undeclared {
                name: tool.name.clone(),
            });
        }
    }

    // 同名工具 schema 比对（按声明顺序）：仅 manifest 显式声明了 schema 才比对
    for tool in declared {
        let Some(declared_schema) = &tool.input_schema else {
            continue; // 声明未覆盖 schema → 以实际为准（存量插件代码内 schema 模式）
        };
        if let Some(actual_tool) = actual.iter().find(|a| a.name == tool.name) {
            if *declared_schema != actual_tool.input_schema {
                mismatches.push(VerifyMismatch::SchemaMismatch {
                    name: tool.name.clone(),
                    declared: declared_schema.clone(),
                    actual: actual_tool.input_schema.clone(),
                });
            }
        }
    }

    mismatches
}

/// 报告汇总：插件级视角（missing 计数 → 拒绝注册的工具名集合）。
///
/// "拒绝该条贡献"落地的辅助：安装校验发现漂移后，调用方按此集合从注册中
/// 剔除对应工具（其余能力照常注册）。
pub fn rejected_tool_names(mismatches: &[VerifyMismatch]) -> std::collections::HashSet<String> {
    mismatches
        .iter()
        .filter_map(|m| match m {
            VerifyMismatch::Missing { name } | VerifyMismatch::SchemaMismatch { name, .. } => {
                Some(name.clone())
            }
            VerifyMismatch::Undeclared { .. } => None,
        })
        .collect()
}

// ═════════════════════════════════════════════════════════════════
// 单元测试
// ═════════════════════════════════════════════════════════════════

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn declared(name: &str, schema: Option<serde_json::Value>) -> ToolCapability {
        ToolCapability {
            name: name.to_string(),
            description: Some(format!("tool {name}")),
            input_schema: schema,
            output_schema: None,
            category: None,
            ui: None,
            render: None,
            smoke: None,
        }
    }

    fn actual(name: &str, schema: serde_json::Value) -> ActualTool {
        ActualTool {
            name: name.to_string(),
            description: Some(format!("tool {name}")),
            input_schema: schema,
        }
    }

    #[test]
    fn consistent_declared_and_actual_is_clean() {
        let schema = json!({"type": "object", "properties": {"a": {"type": "string"}}});
        let d = vec![declared("t1", Some(schema.clone()))];
        let a = vec![actual("t1", schema)];
        assert!(compare_tools(&d, &a).is_empty());
    }

    #[test]
    fn declared_but_missing_is_detected() {
        let d = vec![declared("ghost", None)];
        let a = vec![actual("real", json!({}))];
        let m = compare_tools(&d, &a);
        // 双向漂移：ghost 声明有实际无（missing）+ real 实际有声明无（undeclared）
        assert_eq!(
            m,
            vec![
                VerifyMismatch::Missing {
                    name: "ghost".into()
                },
                VerifyMismatch::Undeclared {
                    name: "real".into()
                },
            ]
        );
        // missing → 拒绝注册（undeclared 不拒绝）
        let rejected = rejected_tool_names(&m);
        assert!(rejected.contains("ghost"));
        assert!(!rejected.contains("real"));
    }

    #[test]
    fn undeclared_actual_is_detected_but_not_rejected() {
        let d = vec![declared("t1", None)];
        let a = vec![actual("t1", json!({})), actual("sneaky", json!({}))];
        let m = compare_tools(&d, &a);
        assert_eq!(
            m,
            vec![VerifyMismatch::Undeclared {
                name: "sneaky".into()
            }]
        );
        assert!(
            rejected_tool_names(&m).is_empty(),
            "未声明暴露不拒绝注册（本就不注册）"
        );
    }

    #[test]
    fn schema_mismatch_is_detected() {
        let d = vec![declared(
            "t1",
            Some(json!({"type": "object", "properties": {"a": {"type": "string"}}})),
        )];
        let a = vec![actual(
            "t1",
            json!({"type": "object", "properties": {"b": {"type": "number"}}}),
        )];
        let m = compare_tools(&d, &a);
        assert_eq!(m.len(), 1);
        match &m[0] {
            VerifyMismatch::SchemaMismatch { name, .. } => assert_eq!(name, "t1"),
            other => panic!("expected SchemaMismatch, got {other:?}"),
        }
        assert!(
            rejected_tool_names(&m).contains("t1"),
            "schema 漂移 → 拒绝该条贡献"
        );
    }

    #[test]
    fn undeclared_schema_skips_comparison() {
        // manifest 未声明 schema（None）→ 不比对（存量插件代码内 schema 模式，
        // 以实际为准）——无论实际上报空对象还是完整 schema 都一致
        let d = vec![declared("t1", None)];
        assert!(compare_tools(&d, &[actual("t1", json!({}))]).is_empty());
        assert!(
            compare_tools(
                &d,
                &[actual(
                    "t1",
                    json!({"type": "object", "properties": {"a": {}}})
                )]
            )
            .is_empty(),
            "None 声明跳过比对"
        );
    }

    #[test]
    fn explicitly_empty_schema_vs_real_schema_is_mismatch() {
        // 显式声明空对象而实际有参数 → schema 漂移（声明了就要严格）
        let d = vec![declared("t1", Some(json!({})))];
        let a = vec![actual("t1", json!({"type": "object"}))];
        assert_eq!(compare_tools(&d, &a).len(), 1);
    }

    #[test]
    fn key_order_insensitive_schema_comparison() {
        // serde_json Map 比较键序无关：同 schema 不同键序 → 一致
        let d = vec![declared(
            "t1",
            Some(json!({"properties": {"a": {"type": "string"}}, "type": "object"})),
        )];
        let a = vec![actual(
            "t1",
            json!({"type": "object", "properties": {"a": {"type": "string"}}}),
        )];
        assert!(compare_tools(&d, &a).is_empty());
    }

    #[test]
    fn parse_actual_tools_extracts_fields() {
        let raw = json!({
            "tools": [
                {"name": "a", "description": "desc a", "inputSchema": {"type": "object"}},
                {"name": "b"},
                {"name": 42},
                "not-an-object",
            ]
        });
        let (tools, malformed) = parse_actual_tools(&raw);
        assert_eq!(malformed, 2, "缺 name 与畸形项计入 malformed");
        assert_eq!(tools.len(), 2);
        assert_eq!(tools[0].name, "a");
        assert_eq!(tools[0].description.as_deref(), Some("desc a"));
        assert_eq!(
            tools[1].input_schema,
            json!({}),
            "缺 inputSchema 归一空对象"
        );
    }

    #[test]
    fn parse_actual_tools_missing_tools_key_is_malformed() {
        let (tools, malformed) = parse_actual_tools(&json!({"foo": 1}));
        assert!(tools.is_empty());
        assert_eq!(malformed, 1);
    }

    // ── Phase 1-C5：services 入参形状（插件↔服务）比对 ─────────────────

    #[test]
    fn declared_with_services_carries_service_schemas() {
        // 服务 schema auto-backfill 进 services 条目后，必须进入"声明对照集合"
        let v = json!({
            "id": "svc_host", "name": "S", "version": "1.0.0",
            "plugin_type": "system", "language": "python",
            "host_type": "sidecar", "entry": "x",
            "capabilities": {"services": [
                {"name": "svc.foo", "description": "d",
                 "input_schema": {"type": "object", "required": ["a"],
                                  "properties": {"a": {"type": "string"}}},
                 "output_schema": {"type": "object"}}
            ]}
        });
        let m: PluginManifest = serde_json::from_value(v).unwrap();
        let declared = declared_with_services(&m);
        assert_eq!(declared.len(), 1);
        assert_eq!(declared[0].name, "svc.foo");
        assert!(declared[0].input_schema.is_some(), "服务入参 schema 进对照集合");
        assert!(declared[0].output_schema.is_some());
    }

    #[test]
    fn service_input_schema_mismatch_is_drift() {
        // 服务声明了入参形状、提供方实际暴露不符 → 漂移（SchemaMismatch）
        let v = json!({
            "id": "svc_host", "name": "S", "version": "1.0.0",
            "plugin_type": "system", "language": "python",
            "host_type": "sidecar", "entry": "x",
            "capabilities": {"services": [
                {"name": "svc.foo", "description": "d",
                 "input_schema": {"type": "object", "required": ["a"],
                                  "properties": {"a": {"type": "string"}}}}
            ]}
        });
        let m: PluginManifest = serde_json::from_value(v).unwrap();
        let declared = declared_with_services(&m);
        let actual = vec![ActualTool {
            name: "svc.foo".to_string(),
            description: None,
            input_schema: json!({"type": "object"}),
        }];
        let mm = compare_tools(&declared, &actual);
        assert_eq!(mm.len(), 1, "服务入参形状声明 vs 实际不符 → 必须漂移");
        match &mm[0] {
            VerifyMismatch::SchemaMismatch { name, .. } => assert_eq!(name, "svc.foo"),
            other => panic!("预期 SchemaMismatch，实际 {other:?}"),
        }
        assert!(
            rejected_tool_names(&mm).contains("svc.foo"),
            "服务形状漂移 → 拒绝该条贡献（与工具通道一致）"
        );
    }
}
