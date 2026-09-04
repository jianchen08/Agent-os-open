# 任务待办清单

## 一、方向校准
- [x] ✅ 拆解 8 个调研点为可执行的问题列表（已写入 research_questions.md）

## 二、信息收集（按问题搜索）
- [x] ✅ Q1: Godot 4 标准项目结构 — 来源 3（Autoload 文档）
- [x] ✅ Q2: 玩家移动实现 — 来源 1、4、7（CharacterBody2D + InputEvent + Input examples）
- [x] ✅ Q3: 障碍生成两种模式对比 — 基础原理已收集 + 本地工程知识补充
- [x] ✅ Q4: 碰撞检测方案选型 — 来源 1、2、8（CharacterBody2D + Physics intro + Area2D）
- [x] ✅ Q5: 物理层（collision layer/mask）规划 — 来源 2（Physics introduction）
- [x] ✅ Q6: 难度递增策略 — 阶梯 + 软上限，🟡C 行业实践补充
- [x] ✅ Q7: 计时器与 HUD 实现 — 来源 5（Timer 类参考）
- [x] ✅ Q8: 失败流程与重试 — 来源 3、6（Autoload + SceneTree）

## 三、闭环校验
- [x] ✅ 检查 8 个问题全部覆盖（见 research_notes.md 覆盖度表）

## 四、报告撰写
- [x] ✅ 撰写 docs/working/speed_dodge_research.md（428 行，10 章节）
- [x] ✅ 末尾给出"对本项目的推荐方案"总结（5 维度：场景结构/脚本分工/物理层/难度递增/失败重试）
- [x] ✅ 总行数 428 行 < 600 行限制

## 五、自评审计
- [x] ✅ 逐项检查验收标准
  - 产出路径正确（docs/working/speed_dodge_research.md）
  - 8 个调研点章节齐全
  - 每章含 5-15 行 GDScript 代码骨架
  - 末尾推荐方案覆盖 5 个维度
  - 行数 428 < 600
  - 8/9 来源为 🟢A 级（占比 89% > 50%）
  - 矛盾信息单独章节标注
  - 来源按「结论」← 来源[等级] 格式绑定
- [ ] 🔜 调用 task_evaluate 提交评估
