---
name: 环境配置决策
description: 环境安装与配置的决策规则，environment_setup_agent 加载后据此判断配置策略。涵盖依赖层级判定、隔离模式选择、三种安装方法决策树、环境阻碍自诊与修复（任务撞环境类失败时的识别-定方案-修复-持久化闭环，所有任务适用）。
---

# 环境配置决策

> **本技能是流程指引，不是逐条打勾清单**：按场景判断使用，遇不适用情况保持裁量，不机械执行。

## 适用场景

environment_setup_agent 接到环境安装/配置任务时加载本 skill，据此判断每个依赖该装到哪里、用什么隔离模式、用什么方法安装。

## 隔离模式基础（来自代码事实）

系统有两种隔离级别（由 isolation_guard 容器机制落地）：

| 隔离级别 | 枚举值 | 执行环境 | 能否操作 docker |
|---------|--------|---------|----------------|
| CONTAINER | `isolated` | Docker 容器内，无 docker daemon | ❌ 不能 |
| HOST | `non_isolated` | 宿主机直接执行，可访问 docker daemon | ✅ 能 |

**关键**：docker commit / docker build 只在 HOST（non_isolated）模式下可用，因为容器内没有 docker daemon。

## 核心决策模型

收到每个环境依赖，先判断属于哪个层级：

### 一、系统级工具链 → 固化进 agentos 镜像

**判定标准**：多项目共享、版本稳定、不含项目特定配置。

| 类型 | 示例 |
|------|------|
| 运行时环境 | Python、Node.js、Go、Java、Rust |
| 系统工具 | git、curl、wget、build-essential |
| 全局 Python 包 | pyyaml、pytest、ruff、playwright 等预装包 |
| 浏览器/驱动 | chromium、chrome-headless-shell |

**安装方式**：
- **隔离模式**：必须 `non_isolated`（HOST 模式），因为需要操作 docker daemon 做镜像固化
- 固化进镜像有两种方法（详见下文"三种安装方法"）
- **镜像保存由 L1 统一决策**：L1 根据编排报告中的环境变更信息决定是否将当前环境保存为 agentos 镜像。镜像的创建/更新/清理由 L1 执行，因为镜像是跨容器共享资源

### 二、项目级依赖 → 装到 workspace

**判定标准**：仅当前项目使用、含项目特定版本/配置。

| 类型 | 示例 |
|------|------|
| 项目依赖包 | package.json 中的依赖、requirements.txt 中的包 |
| 项目配置文件 | .env、config.yaml、tsconfig.json |
| 项目级虚拟环境 | 某项目专用的 Python venv |

**安装方式**：
- **隔离模式**：`isolated`（容器模式）即可，在隔离副本中装到 workspace
- workspace 通过 bind mount 挂载到容器内 `/workspace`，容器内安装的依赖写入挂载目录，容器销毁后仍保留，新容器立即可用
- workspace 必须指向项目根目录，不能指向子目录或临时路径

## 三种安装方法决策树

确定了依赖层级后，选择具体安装方法：

```
该依赖属于哪个层级？
│
├─ 系统级（要进镜像）
│  → non_isolated 模式执行
│  ├─ 是否可版本化、可审查的批量变更？
│  │  ├─ 是 → 方法二：Dockerfile 构建（推荐，可追溯）
│  │  └─ 否 → 方法三：docker commit（快速但不可回滚）
│  → 安装后在报告中标注"已固化/建议固化为镜像"
│
└─ 项目级（装 workspace）
   → isolated 模式即可
   → 方法一：工作区持久化（推荐，最常用）
```

### 方法一：工作区持久化（推荐，项目级依赖首选）

在 workspace 目录内安装依赖，因为该目录 bind mount 到容器 `/workspace`，容器内立即可用：

```bash
# Python 依赖 — 装进虚拟环境（推荐）
python -m venv {workspace}/.venv
{workspace}/.venv/bin/pip install <package>

# Node 依赖 — 装到 workspace 内
cd {workspace} && npm install <package>
```

优点：轻量、不影响其它任务、容器重建后依赖仍在（写在挂载目录里）。

### 方法二：Dockerfile 构建（系统级依赖，可追溯）

编辑 `docker/agentos/Dockerfile` 添加依赖，然后重建镜像：

```bash
docker build -t agentos:latest -f docker/agentos/Dockerfile docker/agentos/
```

适合可版本化、可审查的批量变更。重建后需销毁已有容器才会生效。

### 方法三：docker commit（系统级依赖，快速但不可回滚）

将运行中容器的可写层提交为新镜像：

```bash
# 1. 找到容器名（格式：cua-<workspace路径最后一段>）
docker ps --filter "name=cua-"

# 2. 在容器内安装依赖
docker exec <容器名> apt-get install -y <package>

# 3. 清理容器内临时文件和缓存（commit 会保存整个可写层）
docker exec <容器名> sh -c "rm -rf /tmp/* /var/cache/* /root/.cache/*"

# 4. 提交为新镜像
docker commit <容器名> agentos:latest

# 5. 销毁旧容器（commit 后当前容器仍是旧镜像层，必须销毁重建才生效）
docker rm -f <容器名>
```

**关键约束**（必须遵守）：
- 覆盖 `agentos:latest` 后旧镜像不可回滚（被 docker image prune 清理）
- commit 保存容器整个可写层，包括非预期改动——提交前必须确认容器状态干净
- commit 后仅对之后新建的容器生效，已运行容器不会自动应用

## 镜像管理职责划分

| 角色 | 职责 |
|------|------|
| environment_setup_agent | 执行安装操作、在报告中标注哪些依赖建议固化为镜像 |
| L2 编排者 | 在编排报告中汇报环境变更信息（什么环境、什么版本、用途） |
| L1 | 根据编排报告决定是否将环境保存为镜像，执行镜像的创建/更新/清理 |

## 输出要求

环境配置完成后，在产出报告中必须包含：

| 字段 | 说明 |
|------|------|
| 依赖名称 | 安装了什么 |
| 版本 | 具体版本号 |
| 依赖层级 | 系统级 / 项目级 |
| 安装位置 | 镜像 / workspace |
| 隔离模式 | non_isolated（系统级）/ isolated（项目级） |
| 安装方法 | 工作区持久化 / Dockerfile 构建 / docker commit |
| 用途 | 为什么需要这个依赖 |

## 环境阻碍自诊与修复（所有任务通用）

> 任务撞到环境类失败（起服务失败/工具找不到/密钥缺失/超时挂起/二进制缺失/
> 跨平台跑不通）时的标准动作：**先诊断再动手，禁止不加诊断原地重试；同一点
> 失败 3 次必须熔断上报**（任务纪律承接）。

### 触发时机（条件可判定）

1. 任务涉及「起服务 / 跨平台执行 / 调外部资源 / 用密钥」→ **动手前先跑自检**；
2. 任何命令以环境类错误失败（不存在/拒绝连接/权限/超时）→ **失败后立即自检**；
3. 自检命令：`bash skills/skill-env-config-decision/scripts/env_diagnose.sh`
   （WSL 内/宿主均可跑；输出 OBSTACLE 行=阻碍清单，PASS 行=已满足面）。

### 修复方案制定规则

每个 OBSTACLE 按「根因类别 → 修复动作 → 验证 → 持久化」四步制定方案：

| 根因类别 | 判定特征 | 修复方向 |
|---------|---------|---------|
| **供给性**（东西存在但不在可见面/未加载） | 资源在别处存在，沙箱/工作区看不到 | 补供给到可见面（抄配置到未跟踪路径、加载 env、写 runner） |
| **结构性**（沙箱/平台边界内在不可能） | 边界声明明文排除（如 /mnt 遮蔽、禁网、平台二进制不兼容） | 换执行车道（宿主侧执行 / 换隔离模式 / 复制资源进可见面持久层），**不可原地重试** |
| **策略性**（阈值/等待/顺序不对） | 资源与配置都在，只是时机或参数 | 调参/加长等待窗口/改执行顺序 |

**持久化分级**（修复完成后必做，保证"以后每次都一样"）：
- `一次性`：临时产物，本次验证用完即弃；
- `持久层`：写进 WSL 发行版 home / 工作区未跟踪文件（git 忽略），装一次永久；
- `入库`：可复用脚本/知识回写本技能（scripts/ 或本章条目表）——**每修一类阻碍，
  修复脚本与症状条目必须回填本章**，供后续任务直接命中。

### 首批实证条目（2026-11-14/15 装机隔离验证实测）

| # | 症状 | 根因类别 | 修复 | 验证 |
|---|------|---------|------|------|
| E1 | WSL 沙箱内"找不到 Linux 内核二进制"（实为误判，ELF 在 `kernel/target/release/agentos-kernel`） | 供给性 | 自检脚本核对二进制存在性后再下结论 | `env_diagnose.sh` 检查1 |
| E2 | scratch 内核装载后 LLM 调不通 | 供给性 | 密钥抄入 `user_root/.env`（git 忽略）+ provider 配置在 `config/plugins/llm/llm.yaml`（工作区已有，勿重复抄） | 检查2+3 |
| E3 | 内核 banner 后长时间无进展，验证窗口内到不了可用状态 | 策略性（drvfs 慢装载：96 插件 5-8 分钟，NTFS 宿主 ~14s） | 等待窗口 ≥600s；或 scratch 资源复制到 WSL ext4（~/agentos-scratch/）装载回秒级 | 检查5 + health 轮询 |
| E4 | Linux 内核起后 sidecar（llm_core 等）拉不起 | 供给性（venv 单平台布局：工作区 venv 为 Windows `Scripts/` 布局） | WSL 内跑 venv_provision 双平台同建（同 .venv 补 `bin/python+lib`），装一次永久 | 检查4 + sidecar spawn 实测 |
| E5 | 沙箱内连不上装机内核 9101 | 结构性（WSL NAT：127.0.0.1 不通 Windows 宿主） | 验证勿依赖装机内核——起 scratch 自含内核（Linux ELF + 工作区配置即可全栈自洽） | 检查6 |
| E6 | 沙箱内访问不到模型密钥 | 供给性（沙箱 tmpfs 遮蔽 /mnt，密钥在装机 .env；沙箱**不断网**） | 同 E2（密钥抄进工作区未跟踪文件即沙箱可见） | 检查2 |

**已验证事实基线**（2026-11-15 WSL 实测）：Linux 内核（`kernel/target/release/
agentos-kernel`，ELF x86-64）在 WSL Ubuntu 可正常 boot 并加载工作区 config；
runner 样例 = `skills/plugin-dev/scripts/wsl_scratch_kernel.sh`（自检+起内核+
health 等待+症状输出）；沙箱边界 = workspace-only bind + 家目录秘密遮蔽 +
**不断网**（aos-sandbox 无 --unshare-net）。
