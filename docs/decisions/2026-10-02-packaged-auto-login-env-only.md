# ADR 2026-10-02：装机版自动登录只认 AGENTOS_ADMIN_PASSWORD（env 事实源），废除凭据存档与随机生成

## 背景

装机版自动登录原设计（源仓 ADR 2026-09-28-packaged-auto-admin-login-and-login-modal）：
Electron 主进程首次启动生成密码学随机口令（或收编 ambient `AGENTOS_ADMIN_PASSWORD`），
safeStorage 加密存档 userData（`admin-credential.json`），每次随内核 spawn 注入
`AGENTOS_ADMIN_PASSWORD`，渲染进程无会话时用存档自动登录；应用内改密后经 IPC 回写存档。

实证问题（2026-10-02 用户首启实测 + 代码走查）：

1. **首启死路**：全新安装且未设 ambient `AGENTOS_ADMIN_PASSWORD` 时，存档口令是应用
   自造的随机值，用户无从得知；内核播种账号又恒带 `must_change_password=true`（D1），
   首登强制改密闸要求输入旧口令——用户被拦在一个自己不可能知道的口令后面，且无任何
   UI 通道带出该值。
2. **口令事实源分裂**：存档、env、内核库三处各持一份口令，靠「注入 + env 不符即重置 +
   改密回写」三套机制对齐，任一环节失效即分叉（所谓自愈通道本质是在兜设计自己造成的分叉）。
3. **env 收编语义自我打架**：ambient 常驻时收编每次启动都覆盖存档——应用内改密的结果
   会在下次启动被 env 值重置回滚，改密动作失去意义。

## 决策

**装机版自动登录只认 ambient `AGENTOS_ADMIN_PASSWORD`；env 即口令唯一事实源；
launcher/安装器播种该变量是部署契约，未播种（含空白串）即部署 bug。**

- 删除 `electron/admin-credential.ts`（存档读写/随机生成/safeStorage 信封）、
  `auth:admin-credential:sync` 回写 IPC、preload 与 `electron.d.ts` 对应面、
  渲染进程改密回写调用。
- `kernel-manager` 以 `resolveAutoLoginCredential(env)` 解析凭据：非空 trim 后即自动
  登录口令；为 null 时不自动登录（回落登录框）并 warn 部署契约缺失。
- `buildKernelEnv` 不再注入 `AGENTOS_ADMIN_PASSWORD`——ambient 值经原样继承直达内核。
- 内核播种语义配套收敛（`seed_admin_user`）：`must_change_password` 只对随机生成口令
  置位（console 一次性打印、无人持久持有 → 首登强制改密）；env 播种口令是操作员自选
  事实源，不置改密标记。装机版 env 已播种时首启即自动登录进主界面，不再弹改密闸。
- 应用内改密的持久性归 env：env 常驻时，改密结果会在下次启动被内核「env 不符即重置」
  语义对齐回 env 值（操作员模型，与 `docs/guides/deployment.md` 既有语义一致）。

## Alternatives Considered

- **保留存档，改密闸预填旧口令**：治标。闸门仍会出现，且用户被迫改出的新口令会在下次
  启动被 env 重置回滚——强制改密动作本身失去意义。否。
- **保留存档但去掉随机生成（存档只作 env 镜像 + 回写目标）**：ambient 常驻时收编仍每次
  覆盖存档，回写永远被回滚，存档退化为纯冗余；ambient 缺席时又退化回「口令无人知晓」
  死路。两个分支都不自洽。否。
- **未播种 env 时启动即弹错误框（fail-closed 砖死应用）**：对新契约最「正确」，但旧
  launcher 过渡期会把应用直接变不可用，且注册普通账号的兜底路径也被堵死。否，降级为
  warn 日志 + 回落登录框。
- **维持源仓 2026-09-28 现状不动**：首启死路是实证存在的用户可见缺陷，不处置即持续
  触发。否。

## 影响

- 装机版部署契约（新增，硬性）：launcher/安装器必须为应用进程播种
  `AGENTOS_ADMIN_PASSWORD`（用户级环境变量或启动链注入均可）；未播种的应用实例无自动
  登录，主进程 warn，渲染进程回落登录框。该契约须同步进装机 launcher 仓。
- 内核行为变更（影响所有部署形态）：`AGENTOS_ADMIN_PASSWORD` 播种的 admin 不再触发
  首登强制改密（此前 env 播种同样置标记）。随机播种（未设 env）行为不变。
- `auth:admin-credential:sync` IPC 与 `window.electronAPI.adminCredential.sync` 前端
  契约移除（内部消费方全量可感知，按兼容性一刀切不留兼容层）。
- 装机版用户 userData 下遗留的 `admin-credential.json` 成为死文件（无读取方），不主动
  清理（无新增敏感暴露面，随版本自然滞留）。
- 测试：`electron/__tests__/admin-credential.test.ts` 删除，新增
  `auto-login-credential.test.ts`；`authStore.autoLogin.test.ts` 去 sync 用例；内核播种
  生命周期用例断言翻转（env 播种无标记 / 随机播种有标记）。

## 归档

- 实现落点：`electron/kernel-manager.ts`（`resolveAutoLoginCredential`）、
  `electron/main.ts`、`electron/preload.ts`、`frontend/src/stores/authStore.ts`、
  `frontend/src/types/electron.d.ts`、
  `kernel/crates/api/src/bin/agentos-kernel.rs`（`seed_admin_user`）。
- 上游分叉声明：本决策与源仓 ADR 2026-09-28-packaged-auto-admin-login-and-login-modal
  相反，后续 sync 冲突时以本仓为准（重申本 ADR）。
- 被否方案已记录于上方 Alternatives Considered 节。
