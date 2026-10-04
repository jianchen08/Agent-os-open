# ADR 2026-10-03-packaged-user-data-first: 打包版用户数据为准——配置真值、key 解析序与插件裁决收敛用户空间

## 背景

打包版（装机态）被实测出三类与「用户数据为准」相悖的行为（2026-10-03 用户裁决：
**所有数据以用户数据为准，不能读内核侧数据；env 播种缺失即部署问题**）：

1. **配置真值在包内侧**：`buildKernelEnv` 把 `AGENTOS_CONFIG_ROOT` 钉在
   `resources\config`（包内），用户空间 `%APPDATA%\agentos\config` 只有
   `users` 子树（授权名单）参与——其余全配置树（agents/pipelines/llm.yaml/
   models…）读的是出厂包内副本，用户改了不生效、升级即丢。
2. **插件双源裁决内置优先**：`AGENTOS_PLUGIN_SOURCE_PRIORITY=builtin`
   （BUG-55 防陈旧副本）压过用户空间插件副本——与 dev 侧「同 id 用户赢」及
   `sync_installed_user_space` 维护用户镜像的既有流程相悖。
3. **key 解析序环境优先**：`.env` overlay（`env_delta_overlay`）语义为
   「系统环境显式设置的变量不进 overlay、进程环境优先」——实测后果：测试
   拉起打包应用时手工注入 env key，**聊天真实可用但设置页全部显示「未配置」**
   （前端 `has_key` 按用户空间 `.env` 掩码视图判定，配置页 `deriveKeyStatus`），
   UI 真值与运行时解析分叉。

## 决策

三刀收敛，全部对齐「用户空间是唯一真值」：

1. **配置根用户化 + 首启补缺播种**：`ensurePackagedKernelRunning` 播种
   `<userRoot>/config`（`seedTreeMissingOnly`：**仅补缺失文件，绝不覆盖/删除
   既有**——沿用 seedZonePolicyFiles 的非破坏语义），随后
   `AGENTOS_CONFIG_ROOT` 指向用户空间配置根。包内 `resources\config` 降级为
   **出厂种子**，之后永不直接消费；播种失败回退读包内（与旧行为一致的降级）。
2. **插件裁决用户优先**：`AGENTOS_PLUGIN_SOURCE_PRIORITY=user`。BUG-55 的
   陈旧副本治理归安装/同步流程（`sync_installed_user_space` 单向补缺）与
   应用升级，不以压过用户数据的方式兜底。
3. **`.env` overlay 用户数据优先**：`env_delta_overlay` 改为 `.env` 出现的
   变量**一律注入**（覆盖 ambient 同值）；`lookup_env_var` 查找序同步翻转为
   `.env` → 进程环境回退。保留名（`AGENTOS_*`/`PATH` 等系统关键变量）读侧
   兜底不叠加——与内核配置面写侧 `is_reserved_env_name` 同名单，手改 .env
   不得越权改内核行为（写面 422 同名单已拦）。

播种契约总口径：**安装器播种**用户级 `AGENTOS_ADMIN_PASSWORD`（密码学随机，
ADR 2026-10-02-packaged-auto-login-env-only）；**LLM key 由用户经设置页写入
用户空间 `.env`**（`extractApiKeyToEnv` 既有链路）——前者 installer 播种、
后者 UI 引导播种，不存在第三条手工通道；运行时只认用户空间数据。

## Alternatives Considered

- **维持 builtin 优先 + 首启只播 users 子树**（现状）：拒绝——用户改配置
  不生效违背「用户数据为准」裁决本身；升级丢用户配置是更重的缺陷。
- **`.env` 保留「系统环境优先」**：拒绝——正是实测分叉根因；且 `.env` 写面
  已有保留名 422 兜底，翻转后 ambient 唯一残留影响是「部署者可用 env 临时
  覆盖用户 key」，恰属裁决要求禁止的旁路。
- **overlay 读侧不设保留名兜底（信任写面 422）**：拒绝——.env 是文本文件，
  手改绕过写面；内核行为开关（DB_PATH/CONFIG_ROOT/STORAGE_DRIVER…）被配置
  文件改写属安全面退化。
- **插件优先级维持 builtin、另设「用户覆盖开关」**：拒绝——两套语义并存
  即双源漂移温床；裁决口径单一：用户赢。

## 不覆盖保证（用户追加裁决 2026-10-04：打包之后不能覆盖用户已有的配置）

`seedTreeMissingOnly` 为 **add-only**：仅 `!exists(dst)` 时复制，既有文件/目录
零触碰（无 rename/rm/unlink 路径）。三层证据固定为回归门：

- 实机核验：打包版首启播种后，用户空间播种前既存的 25 个配置文件
  （models/plugins/system/tools 子树）mtime 全部早于播种时刻（零改写）；
- 单测「升级场景」：包内配置变更/新增 → 用户已有文件内容一字节不动、
  新出厂文件仅补缺（`337f5a99`）；
- 既有「补缺不覆盖」「复制缺失文件；既有内容绝不覆盖」两用例保持。

已知语义边界（记录不修）：用户**主动删除**某出厂配置文件后，下次启动
播种会将其补回（add-only 无法区分「删除」与「从未有」）；用户**编辑**过的
文件永不回退。若未来需要删除语义，须引入墓碑清单，另行 ADR。

## 影响

- **升级**：既有装机首启即触发全配置树补缺播种（用户已有文件零覆盖）；
  用户空间无 `.env` 时 LLM 显式未配置（显式失败路径，不再可能被 ambient
  残留「意外修好」而掩盖）。
- **测试**：electron kernel-manager 55 passed（断言翻转 + 两新用例）；
  Rust mcp crate env overlay 三态新测 + 查找序断言翻转；集成测试变量
  `AGENTOS_MCP_IT_DOTENV` 更名 `MCP_IT_DOTENV`（新保留名兜底正确拦截
  AGENTOS_* 前缀，原测试变量名撞线）。`incoming_request_dispatch`/
  `custom_request_roundtrip` 两用例在本机失败为既有环境问题（stash 对照
  实证与本次改动无关）。
- **运维**：`sync_installed_user_space` 语义不变（用户空间单向补缺）；
  应用升级不再丢用户配置/授权/key。
- **遗留**：dev 侧（launcher 链）仓库根 `.env` 仍由 `check_env_health`
  维护，与本裁决不冲突（dev 用户根 `.env` 亦为 UI 写面落点）。

## 归档

- 实测事故源：2026-10-03 打包版验证（设置页全显未配置 vs 聊天可用；
  任务矩阵 e2e 与 e2e_02 车道记录）。
- 实现：`3ebe7c48`（内核 env overlay 用户优先 + electron 配置根用户化/插件
  优先级/播种）、`a0f4c91a`（fmt 收尾）。
