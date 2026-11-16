# 项目 CI（.agentos/ci/）

本项目 CI 由 AgentOS 出生装入（底座 base + 类型 pack），随提交自动执法：
`git commit` → `.git/hooks/pre-commit` 薄壳 → `.agentos/ci/run.py`。
物料与本仓解耦（项目自含），本页是唯一需要读的说明书。

## 怎么跑

- 平时不用跑：`git commit` 自动执行 commit lane 门禁，红=拦截，绿=放行。
- 手动全量：`python .agentos/ci/run.py --lane commit`（不传 `--staged` 时 checker
  自行扫描全仓 tracked 文件）。
- 手动单门：`python .agentos/ci/checks/<name>.py`（checker 退出码契约
  **0=绿 / 1=违规 / 2=跳过**，stdout=发现清单）。
- 最近一次运行留痕：`.agentos/ci/last_run.json`（各门结论/耗时/原因）。

门禁分两档：**block**（红即拦截提交）与 **warn**（只报不拦，留痕 last_run.json）。
真例外用 `git commit --no-verify`（勿常态化）。

## 工具缺席 = 跳过，不拦提交

依赖项目工具链的门（如 godot 包的 gdparse/godot headless）在工具缺席时返回 2，
runner 记 skip 与原因，不拦提交；**工具在场但检查出问题 = 红**。

## 怎么补门（local 扩展面，推荐路径）

修一类问题就落一道门——写 `.agentos/ci/local/gates.json` 一条 + checker 放
`local/` 同目录，下次提交即生效，无需任何组装动作：

```json
{
  "gates": [
    {"id": "ban_tmp_logs", "lane": "commit", "tier": "block",
     "run": ["python", "local/checks/ban_tmp_logs.py"], "est_seconds": 1}
  ]
}
```

- 条目 schema：`{id, lane, tier, run, est_seconds}`；`lane` ∈ `commit|task|manual`，
  `tier` ∈ `block|warn`；`run` 是任意命令 argv（**语言槽位**：直接调用该语言现成
  工具，如 `["ruff", "check", "."]`、`["gdparse", <.gd 文件…>]`），退出码遵循
  0/1/2 契约即可接入。
- `local/` 永不被出厂升级覆盖（项目自治优先）；与工厂门同 id 时 local 覆盖并打
  warn 横幅。
- 声明写坏（缺 id/run、tier 越枚举、checker 文件不存在）→ 提交被红拦并报出字段，
  照报错修声明即可。

## 出厂门一览（base 底座）

| 门 | 档 | 管什么 |
|---|---|---|
| no_build_artifacts | block | 构建产物/依赖缓存目录不入库（`__pycache__/`、`node_modules/`、`.godot/` 等） |
| oversized_files | block | 入库单文件 ≤10 MiB（`--max-bytes` 可在 gates 里调） |
| repo_size_budget | block | tracked 总量 ≤200 MiB（`--budget-bytes` 可调，超限列大头） |
| secrets_scan | block | 高置信密钥特征（AWS/私钥块/Google/Slack/GitHub/OpenAI 形态） |
| doc_links | block | md 相对链接与图片引用可达（只管链得通，不管内容） |
| asset_conventions | block | 素材规范引擎：`local/asset_rules.json` 声明才执法，无文件=skip |

类型 pack（如 godot）的门见各 pack README/`gates.json`；升级经 `project.create`
重跑按版本号严格大于覆盖，`local/` 不受影响。

## 素材规范声明（asset_rules.json）

```json
{
  "rules": [
    {"match": ["*.wav", "*.ogg"], "allow_dirs": ["assets/audio"],
     "note": "音频只准入 assets/audio/"}
  ]
}
```

`match` 命中（文件名或相对路径 glob）且不在任一 `allow_dirs` 下 → 违规报出；
删除规则文件 → 该门自动 skip，不红。
