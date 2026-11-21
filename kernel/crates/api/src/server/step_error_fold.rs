//! 步骤失败聚合折叠（ADR 2026-10-15-step-error-run-signature-aggregation）：
//! 未署名完成轮的步失败出口语义——零产出折叠为失败、有产出标有痕完成。

/// 零产出折叠文案：附首条非空插件错误原文（截断 280 字符防异常堆栈淹没），
/// 不做「检查配置与密钥」式固定归因（2026-10-08 用户报告：网络故障曾被误报
/// 成配置问题）。与 `ws_session::no_assistant_reply_content` 同语义同形状——
/// 单源受阻：ws_session 已过文件长度触碰拆分线（>5000 触碰即红）本刀不可
/// 触碰，受限复制由两侧测试各自锁定漂移；ws_session 拆分刀落地时合并单源。
pub(super) fn zero_output_content(plugin_errors: &[serde_json::Value]) -> String {
    let cause = plugin_errors.iter().find_map(|err| {
        let msg = err.get("message")?.as_str()?.trim();
        (!msg.is_empty()).then(|| msg.to_string())
    });
    match cause {
        Some(err) => {
            let brief: String = if err.chars().count() > 280 {
                format!("{}…", err.chars().take(280).collect::<String>())
            } else {
                err
            };
            format!("本轮 run 失败：步骤执行失败且未产生任何回复（{brief}）")
        }
        None => "本轮 run 失败：步骤执行失败且未产生任何回复".to_string(),
    }
}
