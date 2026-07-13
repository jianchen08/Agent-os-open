# syntax=docker/dockerfile:1
# =============================================================================
# AgentOS 0.2 — Docker 镜像（基于 agentos:latest 增量 + Rust 工具链）
#
# 设计原则：
#   1. 不覆盖 agentos:latest — 增量构建为 agentos-0.2:latest
#   2. 复用 agentos:latest 已有的 Python 3.11 / Node.js 20 / 开发工具 / playwright
#   3. 仅增量添加：Rust ≥1.85 + rustfmt + clippy + pkg-config
#
# Multi-stage 结构：
#   Stage 1 (dev)     — 完整开发环境（agentos:latest + Rust），供后续所有 task 使用
#   Stage 2 (builder) — 在 dev 基础上编译内核二进制
#   Stage 3 (runtime) — 仅含内核二进制 + 最小运行时依赖（目标 ≤ 200MB）
# =============================================================================

# ─────────────────────────────────────────────────────────────────────────────
# Stage 1: Dev — agentos:latest + Rust 工具链（增量更新，不覆盖原镜像）
# ─────────────────────────────────────────────────────────────────────────────
FROM agentos:latest AS dev

LABEL maintainer="Agent OS Team" \
      description="AgentOS 0.2 开发环境 — agentos:latest + Rust ≥1.85" \
      stage="dev"

# ── 安装 Rust ≥1.85 + 补齐 pkg-config ──
# agentos:latest 已有: python3.11 / node20 / git / gcc / make / curl / wget / openssl / libssl-dev / playwright
# 缺失: rust / rustfmt / clippy / pkg-config
RUN apt-get update && apt-get install -y --no-install-recommends \
        pkg-config \
        protobuf-compiler \
    && rm -rf /var/lib/apt/lists/*

# 安装 Rust 1.85（官方 rustup 方式，可精确控制版本）
# RUST_VERSION 锁定 1.85.0（jsonschema-rs MSRV 要求 ≥1.85）
ENV RUST_VERSION=1.85.0
ENV RUSTUP_HOME=/usr/local/rustup
ENV CARGO_HOME=/usr/local/cargo
ENV PATH=/usr/local/cargo/bin:${PATH}

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y \
        --default-toolchain ${RUST_VERSION} \
        --profile minimal \
    && rustup component add rustfmt clippy \
    && rustc --version \
    && cargo --version

# ─────────────────────────────────────────────────────────────────────────────
# Stage 2: Builder — 编译 Rust 内核二进制
# ─────────────────────────────────────────────────────────────────────────────
FROM dev AS builder

WORKDIR /build

# 复制最小 Rust workspace（后续 task_03 会替换为完整 workspace）
COPY kernel/ /build/kernel/

WORKDIR /build/kernel

# 编译验证：build + test + fmt + clippy
RUN cargo build --release \
    && cargo test --release --no-run \
    && cargo fmt -- --check \
    && cargo clippy --release -- -D warnings

# ─────────────────────────────────────────────────────────────────────────────
# Stage 3: Runtime — 仅内核二进制 + 最小运行时（目标 ≤ 200MB）
# ─────────────────────────────────────────────────────────────────────────────
FROM debian:bookworm-slim AS runtime

LABEL maintainer="Agent OS Team" \
      description="AgentOS 0.2 内核运行时 — 最小化运行时镜像" \
      stage="runtime"

# 最小运行时依赖：
# ca-certificates: HTTPS 请求证书
# libssl3: OpenSSL 运行时库（内核 HTTPS 客户端）
# libgcc-s1: Rust 二进制依赖的 C 运行时
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        libssl3 \
        libgcc-s1 \
    && rm -rf /var/lib/apt/lists/*

# 从 builder 复制编译好的内核二进制
COPY --from=builder /build/kernel/target/release/kernel-api /usr/local/bin/kernel-api

# 非 root 用户
RUN useradd --create-home --shell /bin/bash kernel
USER kernel
WORKDIR /app

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=5s \
    CMD kernel-api || exit 1

EXPOSE 8080

CMD ["kernel-api"]
