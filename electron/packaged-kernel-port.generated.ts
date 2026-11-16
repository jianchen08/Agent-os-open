/**
 * *** DO NOT EDIT — GENERATED FILE ***
 *
 * 装机版内核端口常量（ADR 2026-09-20-packaged-kernel-port-default-9101）。
 * 由 scripts/generate_port_constants.py 单一真值区生成；手改必被
 * scripts/check_kernel_port_sync.py（重渲染逐字节一致闸）打回。
 * 改端口/主机 = 改生成器真值区 → python scripts/generate_port_constants.py
 * → 两侧生成物随动，连同本文件一起 commit。
 */

/** 打包件内核回源主机（钉回环；回源主机属同刀契约面） */
export const PACKAGED_KERNEL_HOST = "127.0.0.1";

/** 打包件内核默认监听端口（与 dev 栈默认 9100 错峰，两套环境默认并存） */
export const PACKAGED_KERNEL_PORT = 9101;
