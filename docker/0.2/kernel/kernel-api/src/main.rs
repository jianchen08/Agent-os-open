/// 最小 Rust 内核验证二进制 — 验证 Docker 镜像 Rust 工具链完整性
fn main() {
    println!("AgentOS 0.2 kernel — Docker build verification");
    // 验证 serde_json 序列化/反序列化链路
    let json = serde_json::json!({
        "status": "ok",
        "version": env!("CARGO_PKG_VERSION"),
    });
    println!("{}", json);
}
