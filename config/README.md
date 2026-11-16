# 配置目录结构（四域模型）

按 ADR `docs/decisions/2026-09-14-config-ownership-plugin-lifecycle.md`，config/ 按
「谁让它生效」分四个域。每文件恰好一个 owner；任一相对路径任一时刻生效文件恰好一份
（用户空间接管语义见 ADR 2026-09-14 单一存在）。

## 目录结构（2026-09-14 现状）

```
config/
├── kernel/                      # ① 基座域：内核直读，不参与插件装卸
│   ├── storage.yaml             #   存储驱动（env AGENTOS_STORAGE_DRIVER/AGENTOS_DB_PATH 可覆盖）
│   ├── error_codes.json         #   跨端错误码契约（内核+tool_core+前端共同消费）
│   ├── api_config.yaml          #   插件 API 面（⚠️ 无生产读者，死配置嫌疑，待拍板退役）
│   ├── plugin_allowlist.yaml    #   插件准入白名单
│   ├── default_profile.yaml     #   插件启用 profile（用户侧接管写 <USER_ROOT>/config/kernel/）
│   └── kernel_capabilities/     #   内核能力契约 chat/streaming/tool_surface
├── plugins/                     # ② 插件配置域：config/plugins/<plugin_id>/（每插件一命名空间）
│   ├── connectors/              #   godot/vscode/capability_adapters
│   ├── cost_control/ dsh_adapter/ evaluation/ isolation/ llm/ multimodal/
│   ├── review/ tasks/ task_form/ browser/ search/ web_ext/
│   ├── pipeline_spill_guard/ security_check/ model_prompt_adapter/
│   └── ...                      #   manifest config_files 声明 = 资产清单（装卸随插件）
├── tools/builtin_tools_config.yaml  # 例外：自动生成物（collect_tool_info.py），不迁
├── agents/                      # ③ agent 域：agent 配置 + persona + processes
│   ├── main/ orchestrator/ executor/ system/ task/ team/ community/ modes/
│   └── */persona/               # （owner 服务 = agent_manager；热路径读 = 内核注入）
├── pipelines/                   # ④ 管道域：管道组装声明（G10 可视化编辑器写路径）
│   └── autonomous.yaml
├── models/ tools/{web,search,browser}/ system/ isolation/ evaluation/   # （已迁空，留待删除）
├── rules/ templates/ processes/ self_evolve/   # 内容资产：物理位置不动，由消费插件声明引用
└── users/default                # 用户空间播种源
```

## 各域规则

| 域 | owner / 访问权威 | 插件卸载时 |
|---|---|---|
| 基座 kernel/ | 内核直读 | 不参与（鸡生蛋地板） |
| 插件配置 plugins/<id>/ | owner 插件（manifest config_files 声明即资产清单） | 用户插件：随目录删；系统插件：失效不删盘 |
| agent 域 agents/ | agent_manager 服务（写/UI）；context_build（读，内核注入） | 同上双层语义 |
| 内容资产 rules/ 等 | 消费插件声明引用 | 不随插件删除（仓库资产） |

## 内核保留段（插件 config_files 不得映射）

`kernel/`、`plugin_roots`、`auth`、`pipelines`、`steps`
（与 `kernel/crates/api/src/config_service.rs` KERNEL_RESERVED_SEGMENTS、
`scripts/migrate_to_user_root.py` _KERNEL_RESERVED 同源）。

## 配置优先级

1. 用户空间接管文件（`<USER_ROOT>/config/<rel>`，接管登记为准）
2. 环境变量（标量覆盖：驱动/路径/端点/密钥）
3. 出厂文件（本目录，随安装介质升级翻新）
4. 代码默认值（fail-closed 域不回退，显式报错）

## 注意事项

- 敏感信息只落 `<USER_ROOT>/.env`，不入仓
- 插件外挂配置经 manifest `config_files` 声明后由内核注入，禁止代码直读文件
  （机械闸 `check_config_direct_reads.py` 执法，落地见 ADR）
- `tools/builtin_tools_config.yaml` 为自动生成物（`scripts/tools/collect_tool_info.py`），
  勿手改

## 唯一真源声明（P-16，2026-10-29）

仓内曾存在 `config-e2e/`（本机 e2e 场景用的 config/ 逐字副本，曾按「config 同刀」
人工镜像维护）。因无任何脚本/workflow/生产码消费、镜像对主源实质漂移（缺
rules §1.7/§1.8/enforcement_tiers.yaml 等），已于 2026-10-29 整体删除（提案
P-16/E3-6，「消灭副本优先于守护」）。四域模型「任一相对路径任一时刻生效文件
恰好一份」公理恢复成立；e2e 测试的临时配置副本由测试自建（复制主 `config/`
改写，见 `tests/e2e_02/test_task_lifecycle_paths_e2e.py` _build_tmp_config_root），
不依赖仓内副本。如需再建沙箱副本，放 `tests/fixtures/` 并在本文件登记，
禁止回仓根。
