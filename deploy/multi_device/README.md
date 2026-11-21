# deploy/multi_device — 多端统一命名空间（部署接线配方）

> 契约：ADR `docs/decisions/2026-10-18-unified-namespace-fs-mount-and-mcp-transfer.md`
> （D14 路径统一 = OS 文件系统挂载，运行时零路径翻译；D15 组装成熟挂载积木，
> 禁自研文件服务/协议；D16 路径语言 `/<设备名>/...` 三端逐字相同，根下一层直挂；
> D17 每端导出面 × 挂载面；D19 exec 通道收窄 + allowed_roots 平移导出声明 + 凭据三面；
> D20 连接性/读写分离 = 挂载选项，零执法代码）。
> 台账：`docs/working/多端同步与分布式推进_台账.md` R27。

```
统一逻辑命名空间（每端呈现为普通本地路径，进程零路由知识）
        /<设备名>/...（POSIX 形态，三端逐字相同）
   ┌────────┴─────────┬──────────────────┐
/VM-0-16-ubuntu   /DESKTOP-41ST1LM   /mumu-emulator-01
（云端领地）        （桌面领地）         （Android proot 领地）
```

| 端 | 导出（serve） | 挂载（mount） | 锚点（D16） |
|---|---|---|---|
| 云端 Linux 203.0.113.153（VM-0-16-ubuntu） | OpenSSH 内建 SFTP（sshd 已在，零安装） | sshfs over 反向隧道（`agentos-device-mount@.service`） | 根下 `/<设备名>/`（物理即规范） |
| 桌面 Windows DESKTOP-41ST1LM（主力端） | sshd over 反向隧道（`deploy/remote/agentos-fmin-tunnel.service`） | rclone mount（WinFsp）（`desktop_setup.sh`） | 盘根 junction `C:\<设备名>` + Git 安装根 junction `/<设备名>` |
| Android/MuMu（proot 内） | proot 内 rclone serve sftp | proot `-b <挂载目录> /<设备名>` binding（骨架见 §proot，实施待后续刀） | proot 内 `/<设备名>/`（物理即规范） |

本目录五件：

| 文件 | 侧 | 用途 |
|---|---|---|
| `export_declarations.yaml` | 两端 | D19 导出声明（注册表 `exec_channel.allowed_roots` 语义平移；挂载配置生成的读面） |
| `reserved_names.txt` | 登记侧 | 保留名清单初值（设备名注册校验，D16 一句话机制；`desktop_setup.sh` 已消费） |
| `agentos-device-mount@.service` | 云 | sshfs 挂对端领地到 `/<设备名>`（systemd 模板 unit，每设备一实例） |
| `desktop_setup.sh` | 桌面 | 锚点配方脚本（junction 两级 + rclone mount；默认 plan 干跑，`--apply` 才动系统） |
| 本 README | — | 版本钉选 / 凭据三面 / 挂载选项 / 激活步骤与现状 |

---

## §1 版本钉选

- **rclone = v1.75.2**（2026-10-09 stable，安全修复版；三端单二进制：
  serve sftp / mount 全覆盖，Windows 走 WinFsp、Linux 走 FUSE、proot 内可跑）。
- **WinFsp = 2.1.25156**（v2.1 stable，2025-06-05；GitHub releases 最新非
  prerelease——2.2 线尚在 Beta；rclone mount 硬依赖，装机包预带见 §1.1）。
  升级两端独立：WinFsp 驱动层、rclone 应用层，均 CVE 驱动非定期。
- 升级策略：钉 minor（1.75.x 内 patch 跟进安全修复）；CVE 驱动，非定期。
  三端版本必须一致（挂载协议两端版本错位无兼容承诺）。桌面安装走官方
  `rclone-v1.75.2-windows-amd64.zip` 解压入 PATH 或装机包预带；云端
  `rclone` 二进制同版钉选。

## §1.1 桌面依赖预带口径（2026-10-18 用户拍板"预安装放到安装包里"；2026-11-12 拍板安装免管理员）

装机包（NSIS）预带两件：`resources/prereqs/winfsp-2.1.25156.msi` +
`resources/bin/rclone.exe`（备料实物 `electron/prereqs/`，gitignore 不入库；
版本钉选单源=`scripts/check_prereqs.mjs`，electron:build 链首自动直连补齐）。

**提权语义（用户裁定 2026-11-12「安装应用不得使用管理员账户」）**：
`nsis.perMachine=false` + `allowElevation=false` → 安装器 manifest
RequestExecutionLevel user，安装全程普通用户权限，默认落
`%LOCALAPPDATA%\Programs\<产品名>`；双击与 `/S` 无人值守都零 UAC。WinFsp 是
系统级驱动（`msiexec` 必须提权，1018 pack14 实证：非提权上下文静默装失败
exit 103），因此 customInstall 先探当前进程是否提权：**已提权（右键「以管理员
身份运行」/维护轮显式提权）才静默装 MSI；普通用户安装跳过并打印手动安装
路径**，不弹 UAC、不阻断主程序（ADR 2026-11-12-installer-per-user-no-admin）。

**三条安装路径**（`desktop_setup.sh` 同步口径）：

| 路径 | 触发 | 机制 |
|---|---|---|
| ① 预带（默认） | 用户安装灵汐助手 | 已提权安装器内 customInstall 静默装 WinFsp，零补装；普通用户安装跳过并提示路径②；失败不阻断主程序（退出码仅日志）；重复安装 msiexec 1638 幂等跳过 |
| ② 存量修复 | 存量/老包/普通用户安装后的显式动作 | `desktop_setup.sh --bootstrap-winfsp`（winget/官方 MSI，**需提权上下文运行**，已装即跳过）；设置页修复入口待前端刀并入 |
| ③ agent 审批代装 | agent 提审批卡 | "永不自动装"纪律不变：agent 代装/后台场景必须显式审批，任何自动链不得静默代装 |

**预带与"永不自动装"的边界**：预带的知情同意 = **用户安装灵汐助手这个动作
本身**（用户主动装机即知情选择预带依赖；默认安装不提权，提权安装时的 UAC
为其知情面）；"永不自动装"约束的是 **agent 代装与后台静默场景**（需显式
审批）——两者并存不冲突，③路径纪律原样保留。

**per-user 安装语义（2026-11-12 恢复）**：默认安装目录回归
`%LOCALAPPDATA%\Programs\<产品名>`（assisted 界面仍可改，`/D=` 静默强设照旧
生效——维护轮 `/D=D:\agentos-r297-main` 语义不变）；**静默装（/S）不再需要
管理员**。AGENTOS_ADMIN_PASSWORD env 播种仍在安装用户 HKCU。存量 per-machine
安装（Program Files）不会被新包覆盖，需手动卸载旧版一次（用户数据/凭据保留，
见 ADR 2026-11-08-installer-credential-retention）。

**WinFsp 2.1 布局注（1018 装机实证）**：dll/sys 落
`Program Files (x86)\WinFsp\SxS\sxs.<ts>\bin\`（无顶层 `bin\winfsp.dll`），
服务名 `WinFsp.Launcher`（非 winfsp）——`desktop_setup.sh` check_winfsp 两
形态都认，验证/检测勿再误判 MISSING。

**卸载口径**：WinFsp 是系统级驱动，**卸 app 不卸驱动**（卸载器只清 app 与
env 播种；残留驱动对系统无害且后续重装幂等复用）；rclone.exe 为包内载荷，
随 $INSTDIR 删除自动清场，不进系统 PATH。

**诚实标注（预带现状）**：包内 rclone.exe 尚无运行时 PATH 接线（消费面——
electron 主进程把 `resources/bin` 注入 PATH 或 desktop_setup 消费包内 exe——
属后续刀）；现役用户侧 rclone 仍按 §1 手工安装/PATH 路径。

## §2 凭据三面（D19，沿 exec token 双钥先例）

挂载引入**第三类凭据**（文件访问凭据），与 sync token（数据面）、exec token
（执行面）三面分离：

| 面 | 凭据 | 落位 |
|---|---|---|
| 数据面 | sync token（既有） | env `AGENTOS_SYNC_TOKEN`，不入 manifest/注册表/同步面 |
| 执行面 | exec token（既有） | env `AGENTOS_EXEC_TOKEN_*`，用户空间 .env 供给 |
| **文件面（本刀新增）** | SSH 密钥 / rclone 配置 | **各端用户空间**：桌面 `%APPDATA%\rclone\rclone.conf` + 专用密钥 `~/.ssh/<专用密钥>`；云端 `~/.ssh/` + `~/.config/rclone/rclone.conf` |

供给流（与 exec token 双钥同纪律——env/文件引用，明文永不入
manifest/注册表/同步面/仓）：

```bash
# 桌面 → 云端（挂云端领地）：专用密钥一把，公钥入云端 authorized_keys
ssh-keygen -t ed25519 -f ~/.ssh/agentos_mount_cloud -N ""
# rclone 配置落用户空间（key_file 引用密钥文件，密文不进配置体）
rclone config create agentos-cloud sftp host 203.0.113.153 user ubuntu \
  key_file ~\\.ssh\\agentos_mount_cloud

# 云端 → 桌面（挂桌面领地）：云端专用密钥，公钥入桌面 sshd authorized_keys
# （桌面 sshd 装好并立反向隧道后执行，见 §4 激活）
```

## §3 挂载选项与断链语义（D20，零执法代码）

| 语义 | 落点 | 说明 |
|---|---|---|
| 只读快照轴 | `--read-only`（rclone mount）/ `-o ro`（sshfs） | 先例：`deploy/remote/agentos-fmin-local-ro.service` 云侧 ro 模板；执行权限=挂载选项，零自研执法 |
| 读写协作 | 默认 rw（本目录配方取值） | 转只读/读写 = 改 unit 选项 + 重启实例，按需 |
| 掉盘（属主离线） | 进程拿 OS 层错误：ENOENT / EIO / IO timeout | 诚实不可用由文件系统语义天然承载，无应用层判定 |
| 断链无本地写 | 挂载断开即无写路径 | `--vfs-cache-mode writes` 仅挂载在飞期间的写缓冲，不构成离线副本 |
| 并发 | 文件协议自身语义 + 端内既有并发语义 | 裁定①不变 |

## §4 激活步骤（云端先行）与激活状态（2026-10-18）

### 现状探明（只读 SSH 实证，2026-10-18）

| 探明项 | 结论 |
|---|---|
| `agentos-kernel.service` | **enabled + active running（禁碰面，本刀零触碰）** |
| `deploy/remote/` 两 unit（tunnel / local-ro） | **仅模板在库，云端均未部署**（list-unit-files 无、无监听） |
| 云 127.0.0.1:2222 / :9201 | **零监听**——桌面反向隧道未立 |
| 云端 sshfs / rclone | **均未安装**（fusermount 在，FUSE 支持在） |
| 云端 sshd + sftp 子系统 | **active**（导出面零安装可用） |
| 桌面 sshd / rclone / WinFsp | **均未安装**（服务 1060 未安装、无进程、无监听 :22） |
| 云端设备名 | `VM-0-16-ubuntu`（.env 未配 `AGENTOS_DEVICE_ID`，platform.node() 回退口径） |

### 本刀未激活的原因（如实报告）

任务判据为"探明『挂载锚路径调整 + unit 重启』低风险时激活"。实证结论：
**无既有挂载锚可调整、无 unit 可重启**——`deploy/remote` 两模板从未部署，
两端口零监听，两端挂载软件均未安装。激活=从零供给链，需：

1. 云端 `apt install sshfs`（系统级装包，超出"锚路径调整+unit 重启"授权圈）；
2. 桌面装 OpenSSH Server + 立 autossh 反向隧道（密钥供给 + Windows 常驻面，
   属桌面激活刀；模板自带标注"Windows 原生 autossh 替代不在本批范围"）；
3. WinFsp / rclone 桌面安装（任务书明示"不实际安装，待用户知情执行"）。

故**只落实物不激活**；激活步骤全量在案，待用户知情后与 WinFsp/rclone
安装同批执行（低风险前提成立时：kernel 服务零触碰，挂载为纯增面）。

### 云侧激活步骤（前置齐备后执行）

```bash
# 0. 前置：桌面侧隧道在飞（2222 通）、密钥已互信、桌面 sshd 就绪
ssh -p 2222 <desktop-user>@127.0.0.1 'echo ok'   # 隧道连通性实测

# 1. 装 sshfs（一次性）
sudo apt install -y sshfs

# 2. sftp 路径形态实测（占位确认，Windows OpenSSH 形态随实现而异）
sftp -P 2222 <desktop-user>@127.0.0.1   # ls 看暴露根形态（/d/myproject 或 D:/myproject）

# 3. 模板占位替换后落 unit（<desktop-user>、<sftp 路径形态的桌面领地根> 两处）
cp deploy/multi_device/agentos-device-mount@.service /etc/systemd/system/  # 云端
sudo systemctl daemon-reload

# 4. 按设备名启用实例（导出声明见 export_declarations.yaml）
sudo systemctl enable --now agentos-device-mount@DESKTOP-41ST1LM

# 5. 验证（D16 验收口径：/<对端设备名>/ 出对端领地内容）
ls /DESKTOP-41ST1LM/myproject   # 出桌面 D:\myproject 内容即通
```

### 桌面侧激活步骤

```bash
cd deploy/multi_device
./desktop_setup.sh --check                                       # 依赖体检
winget install -e --id WinFsp.WinFsp                             # WinFsp 知情安装（§脚本头两路径）
# rclone v1.75.2 安装后（凭据配置见 §2）：
./desktop_setup.sh VM-0-16-ubuntu --remote agentos-cloud:ubuntu  # 先 plan 干跑看动作
./desktop_setup.sh VM-0-16-ubuntu --remote agentos-cloud:ubuntu --apply   # 管理员 Git Bash
# 验收（D16）：bash 内逐字读写
echo test > /VM-0-16-ubuntu/multi_device_验收文件 && cat /VM-0-16-ubuntu/multi_device_验收文件
rm /VM-0-16-ubuntu/multi_device_验收文件
```

## §5 保留名清单（D16）

- 清单：`reserved_names.txt`（Linux 根系统目录 + MSYS 根特有件 + Windows 盘根
  保留名 + DOS 设备名，四类来源见文件内注释）。
- 执法点：**设备名注册时**逐名比对（Windows/macOS 大小写不敏感，Linux 精确），
  命中即拒/要求改名——不是新机制，一句清单执法；`desktop_setup.sh` 锚定前
  同律校验（已实证：`etc` 被拒）。

## §6 proot 骨架（Android，实施待后续刀）

app-mobile KernelHostPlugin 的 proot 双 bind 机制即 Android 承载（ADR D17
实证在案）。后续刀待办（先立骨架，禁本刀实施）：

```bash
# proot 启动参数追加（rootfs 种子布局同步登记挂载目录）：
proot -b <挂载目录> /<设备名> ...     # 他端领地绑进 rootfs /<设备名>（物理即规范）
# proot 内跑 rclone/sshfs 客户端挂远端（Linux 二进制，rootfs 已有 glibc 栈）
# proot 内 rclone serve sftp 暴露本端工作空间（导出面）
```

诚实标注（ADR D17）：挂载活动全部发生在 proot 会话内，proot 外进程域
（app Java 侧/WebView）不可见——该进程域走 D18 MCP 转接或 app 侧桥；
MuMu 共享文件夹旁路与 proot I/O 开销待 T3 真机基准后写入本配方。

## §7 验收口径（D16 定版）

三端各跑一次 file 读写 `/<对端设备名>/测试文件`：路径字符串逐字一致 +
文件落在对应属主端磁盘。bash 验收面（桌面）：

```bash
echo 多端验收 > /VM-0-16-ubuntu/测试文件 && cat /VM-0-16-ubuntu/测试文件 && rm /VM-0-16-ubuntu/测试文件
```
