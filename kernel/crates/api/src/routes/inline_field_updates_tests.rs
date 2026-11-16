use super::*;

fn collect(
    data: serde_json::Value,
    declared: &[&str],
) -> Result<Vec<(String, serde_json::Value)>, String> {
    let set: std::collections::HashSet<&str> = declared.iter().copied().collect();
    let mut out: Vec<(String, &serde_json::Value)> = Vec::new();
    let obj = data.as_object().expect("data 须为对象");
    for (k, v) in obj {
        collect_inline_field_updates(k, v, &set, &mut out)?;
    }
    Ok(out.into_iter().map(|(n, v)| (n, v.clone())).collect())
}

/// 嵌套视图（GET 同形）整体收集：扁平键 + 嵌套子树混合，叶路径齐入列。
#[test]
fn nested_view_and_flat_keys_mixed_collected() {
    let updates = collect(
        serde_json::json!({
            "compress_trigger_ratio": 0.1,
            "budgets": {"recent": 0.2, "l1": 0.3}
        }),
        &["compress_trigger_ratio", "budgets.recent", "budgets.l1"],
    )
    .unwrap();
    let names: std::collections::HashSet<&str> = updates.iter().map(|(n, _)| n.as_str()).collect();
    assert_eq!(
        names,
        ["compress_trigger_ratio", "budgets.recent", "budgets.l1"]
            .into_iter()
            .collect(),
        "声明的全部叶路径都应入列"
    );
    let get = |n: &str| {
        updates
            .iter()
            .find(|(name, _)| name == n)
            .map(|(_, v)| v.clone())
            .unwrap()
    };
    assert_eq!(get("budgets.l1"), serde_json::json!(0.3));
}

/// 扁平点分键（形态二）单独收集。
#[test]
fn flat_dotted_key_collected() {
    let updates = collect(
        serde_json::json!({"budgets.l1": 0.3}),
        &["budgets.recent", "budgets.l1"],
    )
    .unwrap();
    assert_eq!(
        updates,
        vec![("budgets.l1".to_string(), serde_json::json!(0.3))]
    );
}

/// 路径命中声明名即停（无论值类型）——保持声明字段可写对象值的旧契约。
#[test]
fn declared_name_with_object_value_is_leaf() {
    let updates = collect(serde_json::json!({"cfg": {"a": 1}}), &["cfg"]).unwrap();
    assert_eq!(
        updates,
        vec![("cfg".to_string(), serde_json::json!({"a": 1}))]
    );
}

/// 未声明叶路径 fail-closed，Err 点名完整叶路径。
#[test]
fn undeclared_leaf_rejected_with_full_path() {
    let err = collect(
        serde_json::json!({"budgets": {"nonsense": 1}}),
        &["budgets.recent"],
    )
    .unwrap_err();
    assert_eq!(err, "budgets.nonsense");
}

/// 空对象在未声明前缀下无叶可入列 = 合法 no-op。
#[test]
fn empty_object_at_undeclared_prefix_is_noop() {
    let updates = collect(serde_json::json!({"budgets": {}}), &["budgets.recent"]).unwrap();
    assert!(updates.is_empty(), "空子树应零更新");
}

/// null 落在声明点分路径 = 收集为清除语义（写回侧剥 default）。
#[test]
fn null_leaf_at_declared_path_collected() {
    let updates = collect(
        serde_json::json!({"compression": {"model": null}}),
        &["compression.model"],
    )
    .unwrap();
    assert_eq!(
        updates,
        vec![("compression.model".to_string(), serde_json::Value::Null)]
    );
}
