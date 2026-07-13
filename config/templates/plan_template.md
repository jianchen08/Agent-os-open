# 方案与执行计划模板

> **使用说明（v5.0 产出模型变更）**
> 本模板是 **legacy 参考骨架**，仅供方案总纲 `docs/{title}_solution.md` 的「why 层」组织参考（背景/需求/调研/决策理由/AC 总表/测试蓝图/检查点/变更记录）。
> **具体设计不在此模板范围**：架构/接口/数据模型下沉到 `.project/`，可执行设计下沉到 `docs/tasks/` 任务文件。各领域的产出形状以对应 Skill 的「产出物拆分约定」为准（`skills/skill-solution-*/SKILL.md`）。

***

## 基本信息 \[必填]

| 字段         | 内容               |
| ---------- | ---------------- |
| **方案名称**   | {solution\_name} |
| **方案编号**   | {solution\_id}   |
| **创建时间**   | {date}           |
| **创建者**    | {author}         |
| **需求来源**   | {source}         |
| **优先级**    | {priority}       |
| **方案状态**   | {status}         |
| **计划状态**   | 草稿/已确认           |
| **文档版本类型** | 初稿/正式稿           |

***

## 一、需求概述 \[必填]

### 1.1 背景与目标

**背景**：
{requirement\_background}

**目标**：
{technical\_goals}

**用户价值目标**：
{user\_value\_goals}

### 1.2 需求清单

| 编号    | 需求描述                       | 优先级        | 类型     | 来源       |
| ----- | -------------------------- | ---------- | ------ | -------- |
| REQ-1 | {requirement\_description} | {priority} | 功能/非功能 | {source} |

### 1.3 范围与约束

**范围内**：

- {in\_scope\_item}

**范围外**：

- {out\_of\_scope\_item}

**约束条件**：

- {constraint}

### 1.4 用户旅程与体验要求 \[必填]

```mermaid
flowchart TD
    S1([S-1: {user_step}]) --> S2([S-2: {user_step}])
    S2 --> S3([S-3: {user_step}])
    S3 --> S4([S-4: {user_step}])
```

| 步骤编号 | 用户操作         | 体验指标                 | 失败/异常处理           |
| ---- | ------------ | -------------------- | ----------------- |
| S-1  | {user\_step} | {experience\_metric} | {error\_handling} |
| S-2  | {user\_step} | {experience\_metric} | {error\_handling} |

|加载完成| S2([S-2: 输入账号密码])

> ```
> S2 -->|点击登录| S3{S-3: 校验结果}
> S3 -->|成功| S4([S-4: 登录成功跳转])
> S3 -->|密码错误| E1[/提示剩余尝试次数/]
> E1 --> S2
> S3 -->|验证码错误| E2[/刷新验证码提示/]
> E2 --> S2
> S4 -->|Token 即将过期| S5([S-5: 静默刷新 Token])
> S5 -->|刷新失败| S6([S-6: 跳转登录页])
> ```
>
> ```
>
> | 步骤编号 | 用户操作 | 体验指标 | 失败/异常处理 |
> |----------|----------|----------|--------------|
> | S-1 | 打开登录页面 | 页面加载 < 1s，表单元素清晰可见 | 网络异常时显示重试提示 |
> | S-2 | 输入账号密码并登录 | 点击后 < 500ms 出现加载反馈，登录完成 < 2s | 密码错误提示剩余尝试次数 |
> | S-3 | 登录成功跳转 | 跳转至目标页面 < 1s | Token 刷新失败时静默重试 |
> ```

\-->

***

## 二、调研摘要 \[必填]

### 2.1 现有资源评估

| 资源类型  | 可复用资源                 | 需新建资源            | 备注                |
| ----- | --------------------- | ---------------- | ----------------- |
| Agent | {reusable\_agents}    | {new\_agents}    | {agent\_notes}    |
| 工具    | {reusable\_tools}     | {new\_tools}     | {tool\_notes}     |
| 模板    | {reusable\_templates} | {new\_templates} | {template\_notes} |
| 依赖库   | {existing\_deps}      | {new\_deps}      | {dep\_notes}      |

### 2.2 关键发现

| 编号  | 发现        | 影响       | 来源                  |
| --- | --------- | -------- | ------------------- |
| F-1 | {finding} | {impact} | {source\_reference} |

### 2.3 调研结论

{research\_conclusion}

***

## 三、实施方案 \[必填]

### 3.1 方案概述

{solution\_overview}

### 3.2 结构划分

{structure\_description}

|登录请求| AuthService[AuthService<br/>认证核心]

> ```
> AuthService -->|查询用户| UserRepo[(UserRepo<br/>PostgreSQL)]
> AuthService -->|生成/验证 Token| TokenManager[TokenManager<br/>Token 管理]
> TokenManager -->|读写黑名单| Redis[(Redis<br/>缓存)]
> AuthService -->|返回 Token| Client
> Client -->|携带 Token 请求| AuthService
> AuthService -->|校验 Token| TokenManager
> ```
>
> ```
>
> | 模块 | 职责 | 关键接口 | 依赖 |
> |------|------|----------|------|
> | AuthService | 用户认证核心逻辑 | login(), verify_token() | UserRepo, TokenManager |
> | TokenManager | Token 生成、验证、刷新 | generate_tokens(), validate_token() | Redis |
> | UserRepo | 用户数据访问 | get_user_by_name() | PostgreSQL |
> ```

\-->

### 3.3 关键决策

#### 决策 1：{decision\_topic}

| 备选方案 | 方案描述              | 优势        | 劣势        | 适用场景          |
| ---- | ----------------- | --------- | --------- | ------------- |
| 方案 A | {option\_a\_desc} | {a\_pros} | {a\_cons} | {a\_scenario} |
| 方案 B | {option\_b\_desc} | {b\_pros} | {b\_cons} | {b\_scenario} |
| 方案 C | {option\_c\_desc} | {c\_pros} | {c\_cons} | {c\_scenario} |

**最终选择**：{selected\_option}\
**选择理由**：{selection\_reason}\
**决策因素**：{decision\_factor}

### 3.4 详细设计 \[按需]

***

## 四、验收标准 \[必填]

| 编号   | 验收项                | 指标类型    | 验证方式                   | 优先级   | 关联需求  |
| ---- | ------------------ | ------- | ---------------------- | ----- | ----- |
| AC-1 | {acceptance\_item} | 技术/用户体验 | {verification\_method} | 高/中/低 | REQ-1 |

### 4.1 验收标准清单（机读，唯一真相源）\[必填]

上方表格给人看；下方 yaml 给机器读，是后续执行/门禁/状态矩阵的唯一基准。
**修改验收标准时表格与此 yaml 必须同步**，二者冲突以 yaml 为准。
每条 AC 必须可二值判定；执行阶段细化出的实现级 AC 通过 `traces_to` 指回这里的方案级 AC。

```yaml
acceptance_criteria:
  - id: AC-1
    title: {一句话验收项}
    must: true              # true=必须, false=应该
    category: 功能            # 功能/安全/性能/质量/文档
    verify_hint: {验证方向，不给实现细节}
```

***

## 五、执行评估 \[必填]

- **内容量预估**: {content\_estimation}
- **上下文打包方案**: {packing\_plan}
- **判定依据**: {judgment\_basis}

***

> **其他示例**：
>
> **中篇小说场景**（上下文够，不拆）：
>
> - **内容量预估**: 全书 10 章，共约 57000 字（约 80K tokens）
> - **上下文打包方案**: 包1 = \[第1-10章] → 合并，一个上下文能装下
> - **判定依据**: 全书约 80K tokens，在上下文范围内，合并为一个任务
>
> **网络小说场景**（上下文不够，按卷拆）：
>
> - **内容量预估**: 全书 300 章，约 200 万字（\~3000K tokens）
> - **上下文打包方案**: 按卷拆分，每卷 \~500K tokens 一个包，共 6 个包
> - **判定依据**: 按卷为单位打包，每个包控制在 L2 承载范围内

***

## 六、任务链 \[必填]

### 任务 1: {task\_name}

- **描述**: {task\_description}（描述预期成果和目标状态，不规定实现方法）
- **推荐执行者**: {recommended\_executor}（L2/L3）
- **资源状态**: 已有 / 需新建: {职责和适用场景}（⚠️ 需新建时必须说明：为何现有资源无法复用、新建资源的职责边界是什么）
- **依赖**: 无 / 依赖 {task\_id}
- **验收标准**（每条标注追溯到方案级哪条 AC）:
  - AC-1（traces_to: AC-1）: {acceptance\_criteria}
  - AC-2（traces_to: AC-1）: {acceptance\_criteria}
- **预估时间**: {estimated\_time}
- **上下文环境拆分指引**: （仅 L3 直通时填写）按 知识域/操作域/依赖域 给出拆分建议

### 任务 2: {task\_name}

- **描述**: {task\_description}（描述预期成果和目标状态，不规定实现方法）
- **推荐执行者**: {recommended\_executor}（L2/L3）
- **资源状态**: 已有 / 需新建: {职责和适用场景}
- **依赖**: 依赖任务 {task\_id}
- **验收标准**（每条标注追溯到方案级哪条 AC）:
  - AC-1（traces_to: AC-1）: {acceptance\_criteria}
- **预估时间**: {estimated\_time}
- **上下文环境拆分指引**: （仅 L3 直通时填写）按 知识域/操作域/依赖域 给出拆分建议

***

## 七、执行顺序 \[必填]

```
阶段1（并行）: [任务1 → {执行者}, 任务2 → {执行者}]
阶段2: [任务3 → {执行者}]（依赖任务1、任务2）
```

***

## 八、风险评估 \[可选]

| 编号  | 风险描述                | 影响程度  | 发生概率  | 影响范围    | 应对策略         | 负责人     | 检查点          |
| --- | ------------------- | ----- | ----- | ------- | ------------ | ------- | ------------ |
| R-1 | {risk\_description} | 高/中/低 | 高/中/低 | {scope} | {mitigation} | {owner} | {checkpoint} |

***

## 九、里程碑 \[可选]

| 里程碑         | 包含任务    | 预期时间   | 交付物            |
| ----------- | ------- | ------ | -------------- |
| {milestone} | {tasks} | {time} | {deliverables} |

***

## 十、变更记录 \[必填]

| 版本   | 日期     | 变更内容 | 变更人      |
| ---- | ------ | ---- | -------- |
| v1.0 | {date} | 初始版本 | {author} |

***

## 十一、备注 \[可选]

{notes}

***

## 评估指南 \[可选]

