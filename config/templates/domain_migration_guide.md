# 领域迁移指南

---

## 一、迁移概述 [必填]

> **说明**：描述迁移的领域目标和范围。

- **目标领域**：{domain_name}
- **领域描述**：{领域一句话描述}
- **迁移范围**：
  - 领域规则文件：`config/rules/per_domain/{domain}_domain_rules.md`
  - L2 编排者：{是否需要新建或复用}
  - L3 执行者：{需要哪些执行者}
  - 审查/质检 Agent：{需要哪些审查者}
- **基准文档**：`docs/重要设计/` 下的相关设计文档

---

## 二、阶段一：标杆调研 [必填]

> **说明**：采集该领域的标杆产品/工具/流程，下载实际材料。

### 2.1 标杆采集清单

| 标杆名称 | 类型 | 获取方式 | 关键参考价值 | 材料保存路径 |
|----------|------|----------|-------------|-------------|
| {name} | {工具/流程/规则} | {URL/仓库} | {要借鉴的核心内容} | `docs/working/reference_materials/{domain}/` |

### 2.2 提取笔记

从采集材料中提取：
- 规则结构和关键约束
- 提示词/工作流设计
- 检查点和审查标准
- 错误处理和修复模式

### 2.3 对比分析

| 维度 | 标杆A | 标杆B | 标杆C | 共性模式 | 差异化优势 |
|------|-------|-------|-------|----------|-----------|
| {维度} | ... | ... | ... | ... | ... |

---

## 三、阶段二：领域规则编写 [必填]

> **说明**：基于调研结果编写领域规则文件。

### 3.1 规则文件

- **输出路径**：`config/rules/per_domain/{domain}_domain_rules.md`
- **包含内容**：
  - 领域基线规范（安全底线、质量标准、一致性要求）
  - 产出规范（格式、结构、命名）
  - 审查标准（该领域的法定审查项）
  - 物理保险定义（该领域的自动化验证）
- **不包含**：单个Agent的专属流程（写在Agent自己的system_prompt中）

### 3.2 审查要点

- [ ] 规则文件覆盖了调研发现的所有共性模式
- [ ] 规则与 `docs/重要设计/` 设计文档一致
- [ ] 规则不重复 `document_context_rules.md` 已有的内容
- [ ] 规则不重复编排通用规则已有的内容

---

## 四、阶段三：Agent 配置 [必填]

> **说明**：创建或修改该领域的 Agent 配置。

### 4.1 Agent 清单

| Agent | 操作 | 配置路径 | 领域规则注入 | 备注 |
|-------|------|----------|-------------|------|
| {agent_id} | 新建/修改 | `config/agents/{category}/{agent_id}.yaml` | ✅ | ... |

### 4.2 注入顺序确认

每个 Agent 的 system_prompt 必须按以下顺序组织：

```
位置1: {{path:config/rules/document_context_rules.md}}
位置2: {{path:config/rules/per_domain/{domain}_domain_rules.md}}
位置3: (项目上下文，由系统注入)
位置4: {{path:config/processes/orchestrator_three_phase_pattern.md}}  (仅L2)
       {{path:config/rules/orchestrator_core_principles.md}}       (仅L2)
位置5: Agent本体内容
```

---

## 五、阶段四：验证 [必填]

> **说明**：验证迁移结果的完整性和质量。

### 5.1 格式验证

- [ ] 所有 YAML 配置格式正确
- [ ] 所有规则文件 Markdown 格式正确
- [ ] 注入路径正确（文件实际存在）

### 5.2 内容验证

- [ ] 领域规则覆盖了该领域的核心规范
- [ ] Agent 配置引用了正确的领域规则
- [ ] 去重检查通过（无重复内容）

### 5.3 功能验证

- [ ] L2 编排者能正确调度该领域 L3
- [ ] 审查/质检流程能正常运行
- [ ] 物理保险（自动化验证）能拦截该领域的典型错误

---

## 六、产出物清单 [必填]

| 产出物 | 路径 | 类型 |
|--------|------|------|
| 领域规则 | `config/rules/per_domain/{domain}_domain_rules.md` | 规则 |
| L2 编排者配置 | `config/agents/orchestrator/{orchestrator_id}.yaml` | 配置 |
| L3 执行者配置 | `config/agents/executor/...` | 配置 |
| 标杆材料 | `docs/working/reference_materials/{domain}/` | 参考 |
| 对比分析 | `docs/working/{domain}_comparison.md` | 分析 |

---

