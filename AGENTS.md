# 灵汐 AgentOS — 项目记忆（供 AI 协作者）

> 本文件是给在本仓库工作的 AI 代理（DSH 等）的项目上下文。读完它再动手；
> 口径冲突时以本文件 + `docs/decisions/` + `config/rules/PRIORITY.md`
> （规则面跨文件优先级）为准（README / ARCHITECTURE / guides 已对齐 0.2 现状）。

## 项目是什么

**可进化的智能体操作系统**：把 LLM、工具、记忆、任务、配置组织成可观测、可干预、
可回滚的管道（Pipeline）。当前主线为 **0.2 架构**：Rust 微内核 + Python 插件 +
React 前端。

## 仓库布局（0.2 现实）

| 路径 | 内容 |
|---|---|
| `kernel/` | Rust 微内核（crates：api/config/core/db-admin/engine/hooks/http/invoker/mcp/memory/plugin-loader/seedx/session/tenant/user-admin，另有 integration-tests/native-sdk/native-sdk-test-plugin 支撑 crate；`agentos-kernel` 是主进程） |
| `plugins/` | Python 插件（`shared/<插件id>/` 单层平铺（以 `plugin.json` 计数过百）；`shared/_libs/` 共享测试与管道基类、`shared/_host/` 合宿宿主；ADR 2026-10-13-plugin-flat-layout） |
| `skills/` | 技能包（SKILL.md 规则文件，供 agent 按需装载） |
| `frontend/src/` | React 19 + Vite + Zustand + Antd（RJSF v6 表单引擎） |
| `electron/` | 桌面装机壳（Electron 主进程 + NSIS 打包：内核拉起/托盘/窗口） |
| `config/` | 运行配置（agents/tools/plugins/pipelines/isolation/storage.yaml 等） |
| `deploy/` | Linux 服务器部署实物（systemd 单元/nginx 反代/构建启动脚本） |
| `docs/` | 文档；`docs/decisions/` = ADR 决策记录；`docs/working/` = 工作文档与研究报告 |
| `tests/` | Python 测试（pytest）；`kernel/crates/*/tests/` Rust 测试 |
| `scripts/` | 运维/清理脚本 |
| `data/`、`logs/`、`reports/` | 运行时产物 |

存储：SQLite（默认项目根 `agentos_kernel.db`），driver 化切换见
`config/kernel/storage.yaml`（`AGENTOS_STORAGE_DRIVER`/`AGENTOS_DB_PATH` 环境变量可覆盖）。

## 宿主侧资产登记（ZCode，声明即用、不迁移）

> 注入面契约：本文件对 ZCode 主会话/子代理原生全文注入；AgentOS 挂靠项目时经
> `{{project_agents_md_anchor}}` 注入全文（16384 字符截断）；repo-worker
> 子代理类型=冗余保险，本仓派发优先用。`.zcode/` 不入库。

| 资产 | 一句话用法 | 完整登记 |
|---|---|---|
| 会话记忆库 | 每会话注入 MEMORY.md 索引，按 name 读 .md 正文；与仓内文档冲突以仓内为准；耐久子集已蒸馏进下方〈宿主记忆沉淀〉节 | `docs/working/宿主自动化台账.md` |
| 技能双源 | 仓内 `skills/` 为真源；`.zcode/skills/` 同名件=指针，改动落真源 | 同上 |
| 定时自动化 | 宿主 cron 登记 8 件：现役=自主找活优化轮（每2h，automation-f7b8db3c，进度锚 `docs/working/自主优化轮_台账.md`）+ 装机任务执行监控轮（每30min，automation-41ca8f79，全任务面只读巡检只报不修，台账 `docs/working/装机任务执行监控_台账.md`），其余 6 件历史专项轮暂停待命；**只在 ZCode 宿主触发**，勿在其他通道重复跑；装机维护轮配方真值=`docs/working/装机版维护轮配方_20261013.md` | 同上 |
| 一次性运维配方 | `.zctmp/` 脚本（未跟踪，工作区还原即清）；配方正文以触发器提示词与 docs 为准，被清按配方现写属预期，不抢救 | — |

宿主插件能力（pdf/pptx/xlsx/浏览器自动化/computer-use 等）仅 ZCode 会话可用，
AgentOS 侧任务勿假设其存在；需要时按工具插件立项补位。

## 宿主记忆沉淀（2026-10-19 自 ZCode 记忆库蒸馏）

> 下文是会话记忆库中**耐久裁定与运维铁律**的仓内转写——AgentOS 侧 agent 看不到
> 宿主记忆，以本节为准。全量五组（在飞方案/用户裁定/触发器环境/在飞修复/参考
> 配方）仍以宿主 MEMORY.md 索引为准（仅 ZCode 会话自动注入）；本节不追在飞
> 时效条目，动态进度以仓内台账/ADR 为准。**只收仓内未载的增量**：与既有节
> （开发流程/编码纪律/架构要点/常用操作速查）重复者不收。

### 用户裁定（纪律面，长期有效）

- **全程委托=端到端**：用户说"给你做了/都你做"=ADR、文档同步一并做完一次性交账；仅真新范围才回问；directive 按原话量级取（"替换"=默认路径换血，非 opt-in）。
- **不造轮子接成熟包**：能用成熟方案/外部包就接包不自实现，动手前先盘成熟件；配套口径=暴露≠使用，工具面一次加全。
- **修复必监控拿数据**：修复只落 commit 不算完，须持续监控拿频率数据做修复前后对比；汇报分清"dev 已修未装机"与"修复无效"，指标量化。
- **bug 直修不拿 ADR 挡**：明显错误是 bug 直修，真设计取舍才立 ADR（拿 ADR 流程挡 bug=逃避）；修前先查既有修复史防旧病复发。
- **装机侧动作先问用户**：拉起/重启装机应用等环境动作必须先确认，内核已崩也不许擅自拉；验证改走静态等价证据或用户自验。
- **装机/开发版严禁运行时耦合**：装机版只 spawn 包内内核；开发目录=装机版的外部项目；任何"A 连 B 环境"提案先过此裁定。
- **打包同名同址**：安装器产物名固定+安装目录固定，同址覆盖装；禁每轮版本化新目录（仅隔离诊断才许新目录并说明理由）。
- **连接安全铁律**：脚本操作不得断装机版连接——口令恒读 .env 禁历史值（错误口令触发内核断连非 401）；WS 直发=抢占通道踢前端连接，须 CDP 9222 可用才准用，否则走 REST。
- **产物坐标=执行空间标定**：文件类产物位置一律用 agent 实际执行空间（ws）标定，独立路径字段/启发式定根链退役；state 只存索引字段，大对象本体走 artifacts 文件面。
- **单一键设计禁去重**：建卡 ID=实体 ID；去重认领=设计缺陷补丁（成熟做法同构）。
- **OS 动作走宿主能力**：打开本地路径用 os.startfile 直开；OS 动作套插件壳=故障面。
- **审查报告只写未修项**：已修项一律不进报告正文。
- **开源产品不给用户加内容限制**：分层=出厂自制内容（可自律）→用户实例（不限制）→托管运营（另议）；领域组立组判据=确有积压材料，不预立空组。
- **长跑/小说产出=测试数据**：丢了不抢救不导出；真资产=TUI/插件类长期任务的代码产出。
- **agent 生命周期互斥**：唯一生命周期持有者原则；非持有者任务书死写禁实例操作。
- **模块归属判据=通用潜力非导入数**（todo 设计裁定随附）；todo 用标记路线否工具版（省 LLM 推理调用），capabilities.steps 双步骤单插件。
- **长跑直派契约**：title+description+`inherit_from=<源任务ID>` 接续（整份承接该任务工作空间的 worktree/分支/基座坐标，上下文重新开始）+plain 提示词；任务书禁宿主绝对路径。

### 环境与操作铁律（坑集）

- **git 拓扑**：main 与 dev/0.2 平行历史**无共同祖先**；判远端更改只看 origin/dev/0.2；第三平行线永不 merge 只做内容移植；祖先判别必用 `git merge-base --is-ancestor`（无参 log 显示 HEAD=误读坑）。
- **用户空间两面**：dev=`<repo>/user_root`（gitignored），装机=`%APPDATA%\agentos`；venv 生效面=用户空间副本（BuiltinFirst 用户赢）。
- **播种型插件禁共享根跨包 import**：播种副本对共享根裸模块不可达（import 装机炸）；通道收敛≠代码物理收敛。
- **工作区还原清 venv/node_modules**：症状=electron tsc 报 Cannot find type definition 'node'→仓根 `npm install` 重建；重装装机后固定两步=仓根 npm install+用户空间 venv 镜像进 resources。
- **WSL 铁律**：cargo 必挂 rust:1.88 容器；SQLite 禁落 /mnt/d/；内核跑先复制 ext4。
- **bash 直拉内核=毒化**：动内核只能经 electron；孤儿内核缺密钥注入致 LLM 全断+占 9101 端口，装机启动器拒启；清场=杀全树+正规重启。
- **Windows/打包坑**：NSIS `/S` 静默安装恒带 `/D=<dir>` 否则零文件 exit 0；Git Bash 调 NSIS 前加 `MSYS_NO_PATHCONV=1` 防路径改写；commit 必带 pathspec（裸 commit 扫全仓 874 文件教训）；stash 禁裸 pop、pop 后必 list 验证；`cargo test | tail` / `vitest | tail` 吃退出码=假绿，验门禁勿管道 tail。
- **挂载盘提权盲区**：network-mode 挂载盘（WinFsp/rclone 的 V:）对 RL HIGHEST/提权上下文不可见（`Get-ChildItem V:\` 与 `Win32_LogicalDisk` 双空，UAC 会话隔离覆盖网络重定向器）——挂载健康检查只能用 rclone 进程活性；VBS/wscript 启动失败恒静默（Run 返回 False 无日志），排障先验 exe 路径存在；挂载自愈=幂等脚本（活实例早退永不杀）+计划任务重复触发（登录触发兜底重启）。
- **fake clock**：时序打桩 `setattr(module, "_time", fake)` 于模块命名空间，不改全局 time.monotonic。
- **重启=测试动作非止损**：统一重启顺序（Path 匹配杀到 0 再带参启动）；重启后 resume 端点一发即活进度无损；装机内核日志真身=`resources\kernel\logs\kernel.log.<启动日>`，部署状态须日志实证勿凭记忆。

## ⚠️ 工作区铁律（最重要）

**受管开发环境（本仓容器工作区）会被周期性还原到 git HEAD——未提交的改动会被抹掉**
（普通 clone 无此行为，但提交纪律通用）。任何修改（含中间产物、测试数据）完成后**立即
commit**，不要留到"最后一起提交"。commit 前的调查/验证工作尽量用未跟踪文件（`??`
状态）或文档目录进行。

## 架构要点

- **一切皆插件（架构公理，写文档/方案一律以此为基）**：内核只是执行基座（管道
  解释执行/能力注册表/插件装载/存储），一切能力——LLM、记忆、评估、审批、触发器、
  通道、主题、agent 配置加载——皆插件承载；改业务行为 = 加/改插件或配置，不动
  内核。动因六条与能力→插件对照表见 `docs/ARCHITECTURE.md` 设计哲学 §1。
- **插件即声明**：插件 = `plugin.json` 清单 + Python 实现。`capabilities.tools`
  = LLM 工具**声明即注册**；`capabilities.services` = 内部服务方法元数据。
  工具声明要带 `output_schema` + `render`（tool_core 校验 fail-closed，前端按
  render 意图路由）。
- **评估闸门：插件判定，内核只落库**：评估裁决在 `plugins/shared/evaluation/`
  （task_evaluate 工具），放行检测在管道 output 步骤 task_reminder（提醒耗尽
  仍无评估证据 → 裁决 `task.status = failed`；有证据内核才补落默认
  completed）；内核不做判定，只经 pipeline-state 写面记录结果。agent 配置
  加载同理由管道输入插件 `agent_config_load` 自持。
- **任务默认隔离执行**：默认工作空间 `workspace/{task_id}` + isolated。
- **工具面过滤**：LLM 可见工具由 `config/agents/main/agentos.yaml`（及
  `executor/general_agent.yaml`）的 `tool_ids` 白名单控制，新工具记得加入。
- **权限模式**：4 档（default/accept_edits/auto/bypass；黑名单制：未命中安全
  规则的操作任何档直接执行）+ 参数级危险判定，纯插件前端（http_endpoints +
  form compact + human-interaction 确认）。隔离/worktree 未显式选档时默认
  完全免审批（隔离容器即安全边界；由旁路档承载，与显式选旁路同档同路径）；
  显式选 default/accept_edits/auto 后以所选档为准（隔离不再豁免黑名单），
  显式选旁路即免审批本身；选择器如实显示当前生效档（未选时隔离/worktree 显示
  「免审批」，与显式旁路可辨别）。非隔离会话无免审批默认，按档走。会话隔离由
  isolation_guard 容器落地。
- **插件热发现/热重载全链路**：新建插件目录、修改 plugin.json、改插件 Python
  代码均由 watcher 自动处理（发现→G2 校验→注册/重注册/respawn），无需
  re-enable 或重启；cdylib 集合变更走 G8 自动重启（同 id 换产物保守重启）；
  已知插件面取自共享 manifests 活集合（新插件热注册后管道引用即可编译）；
  前端 schema 需刷新页面才更新。
- **多循环体/执行上下文**：`execution_context` 贯穿任务链；agent_id 的本质是
  执行上下文键——内核无 Agent 运行时对象，语义由插件按键展开（agent_config_load
  按键加载配置，工具面 `tool_ids` 与提示词同源同属此上下文）；内核零 agent
  配置知识（不读 `config/agents/**`）——工具面过滤 = tool_schema 插件读
  state.tool_ids 经内核 `tool-surface` capability 服务完成
  （ADR 2026-09-02-agent-config-decouple-tool-surface）。

## 约定

- **ADR 制度**：任何非平凡决策必须写 `docs/decisions/YYYY-MM-DD-<slug>.md`
  （背景/决策/Alternatives Considered/影响/归档五段），**强制记录被否方案**。
- **契约冻结**：0.2 定型后接口契约尽量冻结（能不动就不动）；动契约需 ADR + 兼容机制。
- **被否方案**：记录在各 ADR 的 Alternatives Considered 节（`docs/decisions/`，按日期排序），动手前先查。
- **门禁**：机械门禁优先（测试/格式/覆盖率）；改造后必须本地补跑全部可验证车道，
  并如实区分"门禁绿"与"测试绿"。
- **触碰即清（治理债随模块清）**：改动触碰某模块（plugins/shared /
  kernel/crates / frontend/src）时，同刀清对应治理债：Python 模块 mypy 错误
  不增、清了就下调 `.github/mypy-baseline.txt`（现值 0，只减不增）；修好基线
  内既有红测试就收紧 `.github/pytest-failure-baseline.txt`（现值
  plugins-coverage 0 / plugins-heavy 0）；新代码带测试（覆盖率棘轮门禁兜底：
  整体基线只升不降且**略高于实测留压力**；执法线见 `docs/working/基线总表.md`
  （自动生成；Python/Rust 目标 90、内核目标 100）；改动行覆盖率 100%（diff
  coverage，`[skip-diff-cov]` 逃生口；检查器 `scripts/check_*_coverage_baseline.py`
  + `scripts/check_diff_coverage.py`））。基线文件改动一律走 commit 留归因。
  细则与三问清单：docs/working/长期治理债清理方案_20260819.md；检查表承接 =
  skills/code-self-review/SKILL.md「检查项（同刀治理债）」（提案 P-28）。

## 编码纪律（编码任务常驻）

> 全文真源 = `config/rules/per_domain/coding_domain_rules.md`（细则/阈值分级/
> 执法口径/示例皆在全文）；本节仅索引锚，冲突以全文为准，跨文件冲突按
> `config/rules/PRIORITY.md` 裁决。

- **需求第一，一字不增**：只实现需求明确描述的功能，禁止"用户可能想…"式假设。
- **最小输出与优先修改**：优先改现有代码而非新增；先查现有接口复用；不整段
  复制（复制必漂移）。
- **错误处理铁律**：要么处理要么传播，禁止吞异常/空 catch/仅日志；错误是值。
- **Bug 修复安全（止血不截肢）**：定位精确错误做最小补丁，严禁退回旧方案或绕开。
- **注释只写现状契约**：翻译式注释删；历史叙事改写为现状（归 git blame 与
  commit message）；`[来源:]`/`[未验证]` 标记保留。
- **测试纪律**：断行为不断实现（mock 仅限外部依赖；时序测试须 fake clock）；
  防拟合（≥2 组有区分度输入、字面值配性质断言、关键路径至少一条真实依赖、
  禁硬编码期望值刷绿）。
- **验证证据独立**：验收测试与验证证据不得由实现方自产；签发的验收测试对实现方只读。
- **探索模式显式声明**：探索产物必须显式标记，不得混入生产路径。
- **兼容性一刀切**：内部代码直接改不留兼容层；仅外部依赖渐进降级（字段只增
  不删、Deprecate→Warn→Remove、禁静默降级）。
- **反模式红线**：硬编码密钥/URL；无注解 any/`@ts-ignore`（须 `// HACK` 注明
  原因）；隐性技术债务（须写明妥协+升级触发+时间上限）；调试日志残留；无测试
  提交；方案倒退（用旧方案须说明为何该场景正确）。
- **攒批提交**：文档/设计类迭代攒批提交，里程碑收口一笔；消息只说改了什么与
  为什么，细节住文档本体（碎片化微提交=噪音，自审清单承接可见化）。
- **Must Fix 阈值**：嵌套 ≤4 层、函数圈/认知复杂度 >25、函数体 >200 行、新增
  Bug/漏洞 >0；覆盖率 P0 核心逻辑 100% 分支 / P1 90% / P2 80%；文件行数机械闸
  （新文件 1500/3000 硬、存量触线 2500/5000，`file-length-baseline` gate）。
- **CJK 行宽纪律（Rust）**：中文每字按 2 列预留 100 列上限，提交前本地
  `cargo fmt`（kernel-fmt 门禁执法）。

## 开发流程（任务固定走法）

**角色分流**：

**调度方**（AgentOS 主会话等）：只做沟通与任务提交，不代执行。活源两种：
**用户提出的开发需求**、台账里的续作候选。
1. **接需求**：澄清到可执行——验收判据明确、改动面清楚；需求第一一字不增。
2. **立卡派发**：task_submit 建任务（后台、隔离工作空间），任务书自含改动面/
   验收判据/相关纪律指针；任务书必须自含「设计真值=<文档路径>」指针（域有
   真值索引的先查索引），无真值指针不派发。任务书另须自含两项（2026-10-29
   装机循环实证后补，用户裁定）：
   - **完成判据指向交付物内容**：判据写成「<目标文件> 里存在 <可验证内容>」
     （某指标条目存在 / 某段被引用 / 某测试文件在位），**不写「落盘台账/
     报告即完成」**——后者会让执行者只交仪式性台账而不交实质（实测台账臂：
     交付物零改动，done.md 宣称完成）。判据是契约，不是读写指令。
   - **接续任务的现场供给**：上次失败的工作空间（半成品代码、.bak、补丁脚本、
     过期台账、审查报告）**不复用**——另起干净工作空间，必要的现状以
     「设计真值/现状摘要」写进任务书。这是供给面处置（决定执行者看到什么），
     不是读写指令（不规定它读哪些文件）。依据：实测同任务同提示词，失败现场
     30 轮零落笔，干净检出第 10 轮真交付；核查集随现场遗留物无界膨胀是唯一
     实测到的驱动因子。
   任务书涉探索/调研类工作时，另须自含**探索模式显式声明**要求——探索任务
   在任务书显式标注，探索产物显式标记、不混入生产路径（依据=编码纪律
   「探索模式显式声明」条，不另立口径）。
   域有对应技能时，任务书同时自含
   **技能索引指针**（如插件新建/更改、门禁与内核联调 →
   `skills/skill-dev-toolkit/SKILL.md`）——技能能力随任务书下发，不挂任何
   agent 提示词（用户裁定 2026-10-19）；执行者按任务书指针 file_read 加载。
   关键路径/新域需求先走需求对抗技能
   （`skills/skill-requirement-battle/SKILL.md`，既有任务书自含技能索引
   指针机制承载，零新件），冻结产物四件套随任务书走。
   新代码任务的 TDD 表述用三步式（用户裁定 2026-10-25）：①Red=设计动作，
   被测是新代码、其 API 契约由测试定义（不确定的外部依赖用最小桩）；
   ②Green 紧跟：实现落盘使测试通过；③外部对接面（注册/声明类）Green 后
   同批落盘，形态不确定处按最合理形态写并在注释标注"假设"。
3. **监控收账**：盯进度与审批卡、验收车道结果、commit 与台账，如实向用户交付。

**执行方**（被派发、在工作空间干活的 agent）四步：
1. **接活立账**：从任务书或台账接活；无台账先建台账作唯一进度锚（四节：轮次
   记录/已修清单/遗留与教训/验收口径；有仓写权限落 `docs/working/<主题>_台账.md`，
   隔离任务落自己工作空间），进度禁口头化。
2. **修 bug 先复现**：失败测试复现 → 归因到根 → 最小补丁 → 回归；禁不加诊断
   直接改码凑绿；同模块打回超 3 次熔断上报，禁止静默绕过。
3. **实现与验证**：按编码纪律与测试纪律执行（全文真源与优先级裁决见「编码
   纪律」节头）；本地补跑全部可验证车道，如实区分"门禁绿/测试绿"；半成品
   不 commit。
4. **落账交接**：完成立即 commit（铁律）+ 台账同刀更新（做了什么/证据/下一轮
   候选）；续作者以台账+前序工作空间为唯一上下文；任务推进修到完，不擅自停
   任务当止损。

## 测试

- Python：`pytest`（部分车道需环境变量，如 DSH e2e = `AGENTOS_DSH_E2E=1`）。
- Rust：`cargo test`（kernel/ 下）。
- 前端：vitest。
- 既有测试在 0.2 迁移后部分断裂属已知，修功能前先确认目标车道基线。

## 常用操作速查

- **构建产物不入库（ADR 2026-10-16-build-artifacts-out-of-git）**：
  `app-mobile/*.apk`、`app-mobile/www/`、根 `pack_linux.tar.gz` 已 gitignore；
  APK 交付走 release/<版本>/ 发行位，git 只承载源码与配方；add 产物路径被拒
  即跳过，**勿 `git add -f` 强推回库**（两远端 100MB 单文件硬限兜底）。
- 加 provider 插件（llm_provider.<type>）：`plugins/shared/llm_provider_<type>/`（provides namespace + 同名 service 背书 + via=plugin 接 llm.yaml；见 ADR 2026-11-04-llm-default-gateway-and-provider-plugins）；网关自路由兜底链=llm.yaml defaults.tiers.fallback_chain
- 加工具插件：`plugins/shared/<name>/`（plugin.json 声明 + 实现 + 测试），
  加入 agentos.yaml 的 tool_ids，补 output_schema/render。
- 加系统插件：`plugins/shared/<name>/`（不再选桶，id 承载语义）。
- 改 agent 配置：`config/agents/main/`（agentos.yaml + persona/ + 提示词骨架）。
- **装机版插件更新（不动装机包）**：`python scripts/sync_installed_user_space.py
  --user-root "$env:APPDATA\agentos" --plugin <plugins/ 下相对路径>`。同 id
  双源裁决=BuiltinFirst（ADR 2026-09-20-packaged-dual-source-adjudication）：
  **插件版本号必须严格大于内置副本**（持平=同步无效）；**绝不重打包、绝不写
  安装目录（脚本硬拒绝）**；生效验证两面查：业务端点新字段 + `/api/v1/schema`
  的 ui_schema 新声明。完整细节（--config/--addon、watcher 热接管、
  disable→enable、venv 供给、asar 前端契约同批）=
  `docs/working/装机版维护轮配方_20261013.md` §10。
- **dev 侧插件 502 排障**：新插件同步进 `user_root/plugins/` 后端点 502 =
  平铺布局 `uv sync` 出界必败且 launcher 静默——**直接复制仓内插件 `.venv`
  即愈**；配置类问题核对内核注入形态（按 manifest `config_files[].id` 命名
  空间嵌套，如 `{"settings": {...}}`）；dev 内核 admin 口令错 = 带
  `AGENTOS_ADMIN_PASSWORD` 环境变量重启 launcher。完整版见配方 §10。
- **打包/部署 CI 门（2026-10-28 裁定）**：打包/部署通过必须是 CI 通过了的
  ——electron:build 链首与 release_all.sh preflight 挂
  `scripts/check_ci_gate.py` 取证 ci.yml + env-gate.yml 全绿（未绿/在跑/
  未跑过均拦）；部署环境检测与打包链实测 =
  `.github/workflows/env-gate.yml`（deploy-env / pack-windows 两车道）。
  ADR 2026-10-28-ci-gated-packaging-deploy。
- **查内核库（任务/管道/追踪数据排查）**：`python scripts/kernel_db_query.py
  "<只读SQL>"`——临时快照只读、对运行中内核安全（装机库 `--db
  "$env:APPDATA\agentos\agentos_kernel.db"`）；键位速查见
  `skills/skill-dev-toolkit/SKILL.md`「内核库查询」。禁止直连活库文件。
- **RP/酒馆域设计真值**：ADR `2026-10-17-rp-authority-spectrum-framework` +
  `docs/working/RP_真值索引.md`（主题→真值文档清单，先查索引再读文）。
- DSH 适配器（源码零改动、插件装载、升级）：`plugins/shared/dsh_adapter/`，
  操作路径与决策见 `docs/working/dsh_decision_records.md`。
- 本项目文档入口：`docs/` 下 ARCHITECTURE.md / vision.md / plugin-protocol.md 等。
