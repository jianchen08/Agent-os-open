//! Lingxi AgentOS 0.2 内核二进制入口。
//!
//! 启动 Axum HTTP/WebSocket API 服务器，提供 /health、/api/v1/* 端点和 /ws WebSocket。
//!
//! 环境变量：
//! - LINGXI_KERNEL_PORT：监听端口（默认 9100）
//! - LINGXI_KERNEL_HOST：监听地址（默认 0.0.0.0）

use std::net::SocketAddr;

use lingxi_api::{routes::AppState, start_server};
use tracing_subscriber::{fmt, prelude::*};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // 初始化日志
    tracing_subscriber::registry()
        .with(fmt::layer().with_target(false))
        .with(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();

    let host = std::env::var("LINGXI_KERNEL_HOST").unwrap_or_else(|_| "0.0.0.0".into());
    let port: u16 = std::env::var("LINGXI_KERNEL_PORT")
        .unwrap_or_else(|_| "9100".into())
        .parse()
        .unwrap_or(9100);

    let addr: SocketAddr = format!("{}:{}", host, port).parse()?;

    info!(target: "lingxi-kernel", "========================================");
    info!(target: "lingxi-kernel", "  Lingxi AgentOS 0.2 内核启动");
    info!(target: "lingxi-kernel", "  监听地址: http://{}", addr);
    info!(target: "lingxi-kernel", "  健康检查: http://{}/health", addr);
    info!(target: "lingxi-kernel", "  WebSocket: ws://{}/ws", addr);
    info!(target: "lingxi-kernel", "  Schema: http://{}/api/v1/schema", addr);
    info!(target: "lingxi-kernel", "========================================");

    let state = AppState::new();
    start_server(addr, state).await?;

    Ok(())
}

use tracing::info;
