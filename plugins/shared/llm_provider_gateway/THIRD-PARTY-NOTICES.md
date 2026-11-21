# 第三方组件声明（THIRD-PARTY NOTICES）

本目录（`plugins/shared/llm_provider_gateway/`）随包分发的第三方组件及许可：

## CLIProxyAPI（网关二进制，bin/ 由打包链按配方填充）

- 上游：<https://github.com/router-for-me/CLIProxyAPI>
- 版本锁定：`scripts/ensure_gateway_binary.py` 的 `GATEWAY_VERSION`（SHA-256 pin
  见同脚本 `SHA256_PINS`，官方 release `checksums.txt` 实证）
- 许可证：**MIT**（GitHub API `license.spdx_id="MIT"` 实证，2026-11-21；
  LICENSE 副本随本插件目录分发，满足保留版权与许可文本的再分发义务）
- Copyright (c) 2025-2005.9 Luis Pater / Copyright (c) 2025.9-present Router-For.ME
- 管理控制台面板（static/，网关运行期按 management.panel-github-repository 自取）：
  [router-for-me/Cli-Proxy-API-Management-Center](https://github.com/router-for-me/Cli-Proxy-API-Management-Center)，MIT。

## 明确不随包（许可红线，查证 2026-11-21）

| 组件 | 上游 | 许可证 | 处置 |
|---|---|---|---|
| workbuddy/qoderwork/traework/zcode provider dll（4 个） | luode0320/cpa-workbuddy-plugin（fork 自 s1sunny/cpa-plugin） | 无许可证（license: null） | **不随包**；本插件 bin/ 仅含 cli-proxy-api 主二进制 |
| zcode-proxy（zcode-api） | TriDefender/zcode-api | CC BY-NC-SA 4.0 | **不随包** |

再分发升级流程：更新 `scripts/ensure_gateway_binary.py` 版本常量前，必须先复核
上游许可证状态（GitHub API license 字段）与本文件结论一致；漂移即停下重新裁决，
禁止先升级后补许可核查。
