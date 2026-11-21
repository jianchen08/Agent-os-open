---
name: 开发工具箱
description: 新建插件、更改插件、跑测试与门禁、内核联调、查内核库/排查任务与管道数据时加载。开发脚本一站式入口：插件脚手架生成（scaffold_plugin.py）、统一门禁 run_gates 用法、scratch 内核实例联调（spawn_test_kernel.py，不拖累现役内核）、内核库只读查询（kernel_db_query.py，临时快照免碰活库）、插件 venv 供给与用户空间同步、声明面同步闸、测试车道速查与真值文档指针。
---

# 开发工具箱

> **定位**：开发/插件/测试/运维脚本的一站式入口（产品裁定 2026-10-19：运营、开发
> 脚本**全部统一放进技能**，技能走到哪脚本就到哪）。双层单源：真源 = 仓内
> `scripts/`（改脚本只改真源）；`scripts/repo/` = `sync_from_repo.py` 生成的
> **字节级精确镜像**（禁手改，提交前 `--check` 必须一致）。技能原生脚本仅 3 个：
> 脚手架、scratch 内核、镜像同步器。

> **分发通道（用户裁定 2026-10-19）**：本技能**不挂任何 agent 提示词**。主通道 =
> 任务书注入——派发插件新建/更改、门禁与内核联调任务时，任务书直接写明本技能路径
> `skills/skill-dev-toolkit/SKILL.md`，执行者按任务书指针 file_read 加载；
> 兜底 = resource_search 按名检索（开发工具箱）。

> **技能 vs LLM 工具判据（扩展本箱/进化提案先过这道）**：调用点由任务书或流程节点
> 决定、`bash` 就能跑的，一律做成技能自带脚本或指挥仓内 `scripts/`；只有
> 需要**模型在运行时自主决策调用**、必须上 LLM 工具面（tool_ids 白名单）、要
> output_schema/render 卡片或权限审批面的能力，才立项做工具。本箱全部资产为技能
> 形态、零工具化（机械核查 2026-10-19：19 技能包 plugin.json=0，本箱 tool_ids
> 登记=0）。

## 〇、脚本镜像（全量随行）

- `scripts/repo/` = 仓内 `scripts/` 的字节级镜像（排除运行垃圾：`__pycache__`/
  `.venv`/`node_modules`/`.ai_workspaces` 会话快照等），随技能快照可达任意工作
  空间/容器（含仓根不可达场景）。
- 同步：`python skills/skill-dev-toolkit/scripts/sync_from_repo.py`；
  验漂移：`... sync_from_repo.py --check`（漂移→退出码 1）。**改过仓内 `scripts/`
  后必须先 sync 再提交**；测试车道有真镜像一致性门检兜底
  （`tests/test_sync_from_repo.py::test_real_repo_mirror_consistent`）。
- 执行注意：镜像解决"可达与携带"；位置感知的门禁脚本（依赖仓根相对布局与基线
  文件）在仓/检出内**仍优先跑仓内真源**（`python scripts/<脚本>`），无 repo 上下文
  环境取用镜像时须自行核对脚本的工作目录假设。

## 一、新建插件（脚手架）

**铁律：新建插件必须在新建目录空间（隔离任务工作空间）内进行**——派发编码任务时
workspace 指向隔离工作空间（仓的完整副本/worktree），脚手架在该空间内生成、测试、
验收后走任务链合并；**禁止在主仓 dev 目录直接长新插件**。本技能脚本随工作空间技能
快照可达，`--target` 默认 = 当前空间根的 `plugins/shared`。

能用本技能脚手架就不再手工起目录；生成后按打印的后续清单走。

```bash
# 工具插件（最常用）：生成完整可跑目录（plugin.json + server.py + 实现 + 测试）
python skills/skill-dev-toolkit/scripts/scaffold_plugin.py my_tool \
  --one-line "一句话描述" --target plugins/shared

# 管道插件：按 config/templates/plugin_scaffold/ 模板渲染类文件
python skills/skill-dev-toolkit/scripts/scaffold_plugin.py my_core \
  --kind core --one-line "一句话描述"   # kind: tool|core|input|output

# 项目 CI 骨架（方案 §3.6 步骤 5）：裸项目一键出提交即闸 + 受保护清单 +
# 执法档位制度 + frontmatter 模板 + 域规范占位（幂等，已存在文件不覆盖；
# git 目标自动 core.hooksPath=scripts/hooks）
python skills/skill-dev-toolkit/scripts/scaffold_project.py <target_dir> \
  --name "项目名" --domain coding   # domain: coding|research|writing
```

生成后必做（顺序）：

1. **tool_ids 登记**：把工具 id 加入目标 agent 的 `tool_ids` 白名单
   （`config/agents/main/agentos.yaml` 或 `config/agents/executor/general_agent.yaml`）——
   不登记则 LLM 工具面不可见；`python scripts/check_agent_tool_ids.py` 验证。
2. **venv 供给**：有第三方依赖时 `uv sync`（或 `python scripts/ensure_plugin_venvs.py`
   全量供给+健康断言）。
3. **测试**：`pytest plugins/shared/<plugin_id>/`（TDD：先 Red 后 Green）。
4. **热发现**：新插件目录/manifest/代码改动由 watcher 自动处理（发现→G2 校验→注册），
   无需重启；G2 校验失败不入面，先修声明再查代码。

工具声明逐字段契约（input_schema/output_schema/render 必填、tool_core fail-closed）
**单源见 `skills/resource-tool-create/SKILL.md`**，本技能不重复。

## 二、更改插件

- **声明面同步闸**（改了 plugin.json 的 http_endpoints / contributes.pages /
  render.card / 工具 schema 后必跑，防前端投影漂移）：
  - `python scripts/gen_frontend_endpoints.py` → `check_frontend_endpoints_sync.py`
  - `python scripts/gen_frontend_plugin_pages.py` → `check_frontend_plugin_pages_sync.py`
  - `python scripts/generate_render_card_vocabulary.py` → `check_render_card_vocabulary_sync.py`
  - `python scripts/check_tool_schema_contracts.py`（声明↔实现对照）
- **依赖变动**：`python scripts/audit_plugin_deps.py`（依赖审计），共享根 import 用
  `python scripts/check_shared_root_imports.py`（播种型插件禁裸名跨包 import）。
- **dev 用户空间同步**：`python scripts/sync_user_root.py --user-root <user_root> --dry-run`
  先预览再落盘（单向 repo→user_root，幂等）。
- **装机版更新**（人工执行，先向用户确认）：`sync_installed_user_space.py`，插件版本号
  必须严格大于内置副本（BuiltinFirst）；细则见
  `docs/working/装机版维护轮配方_20261013.md` §10。

## 三、测试与门禁

**CI 触发纪律（用户裁定 2026-11-06「非必要不手动跑 CI」）**：
CI 由操作自动触发，不由人手动想起——
- 本地提交 → pre-commit 钩子自动跑改动车道快检（阻断式）；
- 本地打包 → `npm run electron:build` 链首自动跑 PACKAGE-FAST 本地取证
  （改动面分档「只跑相关的，恒不全量」，绿后链自动走到出包）；
- git push → 远端 Actions 自动跑（车道分档）。
手动跑 `run_gates.py` 仅限：修红车道时**单车道复验**（`--filter <红车道>`）。
禁止：手动跑全量 `--mode ci`（无此必要——分档已覆盖改动相关面）、手动逐步
复刻打包链、把远端 Actions 当打包门等。

**统一入口（单一事实源，本地与 CI 同口径）**：

```bash
python scripts/run_gates.py --list                # 列出全部门禁
python scripts/run_gates.py --mode fast           # 本地廉价检查
python scripts/run_gates.py --mode plugins        # kernel|plugins|frontend|all
python scripts/run_gates.py --filter plugins-coverage,plugins-diff-coverage
```

- **插件车道含主题色卫生闸**（`frontend-theme-color-hygiene`，2026-10-25 用户
  裁定「颜色一律随主题」）：`--mode plugins` 已并入该闸——plugin.json 声明
  模板（待办卡/表单模板的 inline style）在扫描面内，禁 hex/rgb()/hsl() 字面量，
  须写 `var(--token)`；定义层（主题预设/皮肤）豁免。CI 侧该闸由
  frontend-endpoints-sync job 承载（endpoints 车道触发面含 plugins/**，
  是仅改插件面时的唯一 CI 触达点）。

- **车道前置**：`uv sync --frozen --extra dev --extra isolation` +
  `uv pip install -e ./plugins/sdk`；前端 pnpm install；Rust 走 WSL rust:1.88 容器。
- **覆盖率三查**（触碰即清）：改动行覆盖率 100%（`check_diff_coverage.py`）、
  基线只升不降（`check_python/rust_coverage_baseline.py`）、新代码带测试。
- **假绿红线**：验门禁勿管道 tail（`cargo test | tail` / `vitest | tail` 吃退出码=假绿）。
- 特殊环境变量：`AGENTOS_DSH_E2E=1`（DSH e2e）、`AGENTOS_STORAGE_DRIVER`/
  `AGENTOS_DB_PATH`；WSL 前端车道走 `scripts/run_frontend_tests_wsl.sh`。
- 门禁体系细节（逐门禁语义/执法口径）**单源见 `docs/guides/ci-cd-guide.md`**。

### 内核联调测试（scratch 内核实例，不拖累现役内核）

新插件要连内核做端到端联调时，**禁止把未测插件热加载进现役/装机内核**（那才是拖累）；
用 scratch 内核实例（e2e sanctioned 拉起模式的脚本化，端口/DB/口令三隔离）：

```bash
# 拉起：自动挑空闲端口（硬拒绝 9100/9101）+ scratch DB + 随机口令，轮询 /health 到就绪
python skills/skill-dev-toolkit/scripts/spawn_test_kernel.py spawn --workspace .
# → 输出 instance.json 档案：base_url / port / db / admin_password / run_id

python skills/skill-dev-toolkit/scripts/spawn_test_kernel.py status    # 健康+pid 探针
python skills/skill-dev-toolkit/scripts/spawn_test_kernel.py stop      # 杀实例并验证端口释放
```

- 三隔离：端口≠9100（dev）/9101（装机）；DB 落 `.test_kernel/run_*/kernel.db`；
  口令环境变量注入本实例——不读不改主仓 `.env`/`config/`，scratch 外零文件写。
- 插件根（分级单副本，ADR 2026-11-07）：内核只认 instance.yaml 三根，spawn 具名
  信道 `--instance-config` / `--config-root` / `--user-root`；插件装载验证后端用
  `skills/plugin-dev/scripts/verify_plugin.py`，后端+前端浏览器车道用同技能的
  `verify_host.py --frontend`；WSL 隔离任务经 `verify_host.sh` 走 interop 转发到宿主。
- 前置：内核二进制已构建（`kernel/target/release/agentos-kernel`；WSL 构建**必须**
  挂 rust:1.88 容器）。缺失时脚本报错带构建提示。
- 联调完成后插件走正常合并/同步进主流，scratch 实例用完即 stop。

## 四、内核库查询（kernel_db_query.py，数据排查）

**场景**：开发侧证据采集——排查任务/管道/追踪/会话数据（pipeline_state 键值、
traces 步迹、pipeline_sessions、sessions/message_segments）。

**铁律**：所有查询落在**临时快照**上（VACUUM INTO 一致性快照，与 backup_db.py
同源，对运行中内核安全：不阻塞、不改源、用完即删）；只读多层执法（语句前缀
白名单 select/with/explain/values + 单语句拒绝 + query_only + 行数上限 + 墙钟
超时）。**禁止绕过脚本直连活库文件**。定位边界：本脚本是开发者/脚本侧工具；
插件生产码禁直连内核库（`scripts/check_kernel_db_direct_reads.py` 执法），合法
入口 = db-read 契约（SDK `agentos_plugin_sdk/db_read.py`），两者不互替。

```bash
python scripts/kernel_db_query.py "select name from sqlite_master where type='table'"              # 发现表
python scripts/kernel_db_query.py "select field_key, substr(value,1,120) as v from pipeline_state where pipeline_id like 'd72aa%'"
python scripts/kernel_db_query.py "<SQL>" --db "C:/Users/<u>/AppData/Roaming/agentos/agentos_kernel.db"   # 装机库
python scripts/kernel_db_query.py "<SQL>" --json --limit 500
```

pipeline_state 键位速查（pipeline_id / field_key / value 三列键值面）：

- 任务面：`task.status` / `task.goal` / `task.ws_meta` / `task.owned.<id>.*`（写在提交者管道上）
- 身份面：`agent.id` / `agent_level` / `lineage.parent_pipeline_id` / `ws_meta` / `run_status`
- 步迹面：`traces(pipeline_id, seq, plugin_id, patch_type, patch_data, created_at)`

默认行数上限 200（--limit）、超时 30s（--timeout-s）；大表务必带 WHERE/LIMIT。

## 五、进化（出厂面写保护）

仓根 `skills/` 与出厂插件对 agent 工具直写**运行期恒拒且不可授权**；运行期唯一写路径 =
自进化提案管道（proposal_submit → proposal_apply）。开发期直接改仓由人/AI-in-dev 完成。
用户根（`<user_root>/skills/`）是用户空间，不受此限。

## 六、真值文档指针

| 主题 | 真值 |
|---|---|
| 插件协议（包结构/manifest 契约/种子生命周期） | `docs/guides/plugin-protocol.md` |
| native Rust 插件（cdylib/ABI/G8 重启） | `docs/guides/plugin-native-rust.md` |
| 工具声明逐字段契约 + TDD 流程 | `skills/resource-tool-create/SKILL.md` |
| 门禁体系全文 | `docs/guides/ci-cd-guide.md` |
| 编码纪律/测试纪律全文 | `config/rules/per_domain/coding_domain_rules.md`、`config/rules/testing_rules.md` |
| 项目记忆（布局/速查/铁律） | 仓根 `AGENTS.md` |
