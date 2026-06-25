---
name: 代码自审
description: 编码完成后对本次产出做机械化代码自审（物理保险 5 项检查），产出结构化结果。用于 code_writer_agent 提交 task_evaluate 前的自检，避免每次都派独立 code_reviewer_agent 造成调度开销。
---

# 代码自审

## 用途

编码完成、提交 task_evaluate 之前，对本次改动的代码做**机械化检查**。只跑确定性检查（工具能客观判定，不依赖"第二双眼睛"的逻辑判断），目的是拦截低级错误，省去对小改动派独立审查的开销。

**能力边界**：本技能只覆盖机械检查。需求追溯、架构边界四问、AC 符合度判断仍需独立 code_reviewer_agent。

## 检查项（物理保险 5 项）

| 检查项 | 怎么查 | 判定 |
|--------|--------|------|
| 模块边界物理化 | 检查是否有跨模块非法导入（绕过公共接口直接访问内部实现，如 import `_` 前缀函数、直接访问内部属性） | 有则失败 |
| 架构约束测试 | 检查模块间依赖方向是否正确，是否存在循环依赖（可用 lsp_references / grep import 链） | 有循环则失败 |
| 需求覆盖扫描 | 检查新增代码是否关联了需求来源（AC编号或需求描述），识别无需求的孤儿代码 | 有孤儿代码则失败 |
| 安全与风格Lint | 运行 ruff/flake8/mypy，检查空 catch、遗留 print/console.log、无需求 TODO、硬编码密钥 | 有 error 级则失败 |
| 冗余模式检测 | 检测重复代码、翻译式注释、无效错误处理、死代码 | 有则失败 |

## 执行流程

1. 用 `bash_execute` 跑 lint/类型检查工具（ruff、mypy 等）
2. 用 `enhanced_search` / `lsp_references` 检查跨模块导入和依赖方向
3. 用 `enhanced_search` 扫描无需求 TODO、硬编码密钥、遗留 print
4. 汇总结果，填写执行报告模板的「自审结论」章节

## 产出（填入执行报告）

```yaml
self_review:
  status: pass | fail        # 任一物理保险失败 = fail
  module_boundary: {status: pass|fail, violations: []}
  architecture: {status: pass|fail, cycles: []}
  security_lint: {status: pass|fail, issues: []}
  redundancy: {status: pass|fail}
  must_fix_before_submit: []  # status=fail 时必填，列出必须修的问题
```

## 规则

- `status=fail` 时，`must_fix_before_submit` 中的问题必须全部修复后才能调用 task_evaluate
- 自审结果写入执行报告的「自审结论」章节，供编排器核对
- 自审只覆盖机械检查；本任务是否还需独立 code_reviewer_agent，由执行 Agent 根据风险分级标签判断（见执行报告模板）
