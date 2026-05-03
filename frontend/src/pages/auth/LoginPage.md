# LoginPage

## 需求说明

### 功能概述

登录页面组件，提供用户身份认证入口。核心功能包括：

1. **登录表单**：用户名和密码输入，带标签和必填标识
2. **表单验证**：字段级实时验证（失焦触发）和提交时全量验证
3. **登录请求**：调用 `useAuthStore` 的 `login` 方法发起认证
4. **错误提示**：全局服务端错误（来自 Store）和字段级客户端错误分别展示
5. **加载状态**：提交中禁用所有输入和按钮，按钮文案切换为 "登录中..."
6. **已认证跳转**：检测到用户已登录时自动导航至首页
7. **注册引导**：底部提供注册页面链接

### 用户故事

- 作为未登录用户，我希望在登录页面输入用户名和密码完成身份认证
- 作为未登录用户，我希望在输入框失焦时即时看到验证错误提示，无需等到提交
- 作为未登录用户，我希望在登录失败时看到清晰的错误信息
- 作为未登录用户，我希望在登录过程中看到加载状态，且不能重复提交
- 作为已登录用户，我希望访问登录页时自动跳转到首页，无需重复登录
- 作为未注册用户，我希望在登录页底部找到注册入口

### 验收标准

- [AC1] 页面居中展示登录卡片，最大宽度 448px（max-w-md），垂直水平居中
- [AC2] 页面标题为 "登录"，副标题为 "欢迎回来，请登录您的账号"
- [AC3] 用户名输入框带标签 "用户名"（含红色星号必填标识），placeholder 为 "请输入用户名"
- [AC4] 密码输入框带标签 "密码"（含红色星号必填标识），type 为 password，placeholder 为 "请输入密码"
- [AC5] 用户名为空（纯空格）时失焦验证提示 "用户名不能为空"
- [AC6] 密码为空时失焦验证提示 "密码不能为空"
- [AC7] 验证错误时输入框边框变为 destructive 色，错误文案显示在输入框下方
- [AC8] 表单提交时执行全量验证，验证不通过阻止提交
- [AC9] 服务端错误（useAuthStore.error）以红色圆角卡片形式展示在表单顶部
- [AC10] 加载中（isLoading=true）时所有输入框和按钮禁用，按钮文案变为 "登录中..."
- [AC11] 登录成功后自动导航至首页（ROUTES.HOME）
- [AC12] 已认证用户（isAuthenticated=true）访问此页面时自动跳转至首页
- [AC13] 组件卸载时清除 Store 中的错误状态（clearError）
- [AC14] 底部显示 "没有账号？ 注册" 文案，"注册" 为可点击链接指向 ROUTES.REGISTER
- [AC15] 表单标注 data-testid="login-form"，各元素标注对应 data-testid

## 逻辑说明

### 数据流

```
用户交互
  │
  ├─ 用户名输入 ──→ setUsername ──→ username (State)
  ├─ 密码输入 ──→ setPassword ──→ password (State)
  ├─ 输入框失焦 ──→ handleBlur(field) ──→ validateField(field) ──→ setFormErrors
  └─ 表单提交 ──→ handleSubmit
                     │
                     ├─ validateForm() ──→ 全量验证
                     │     ├─ 失败 → setFormErrors → 阻止提交
                     │     └─ 通过 → 继续
                     │
                     └─ login(username, password)
                           ├─ 成功 → navigate(ROUTES.HOME)
                           └─ 失败 → error 存入 useAuthStore
```

```
useAuthStore
  │
  ├─ login(username, password) ──→ 发起认证请求
  ├─ isLoading ──→ 控制输入框和按钮的禁用状态
  ├─ error ──→ 服务端错误，渲染在表单顶部
  ├─ isAuthenticated ──→ 已认证时触发自动跳转
  └─ clearError ──→ 组件卸载时调用，清除残留错误
```

### 状态流转

**页面初始化：**
```
isAuthenticated 检查
  ├─ true → navigate(ROUTES.HOME)（自动跳转）
  └─ false → 渲染登录表单
```

**表单填写与验证：**
```
字段状态: 空 → 输入中 → 失焦 → 验证
  │
  ├─ 输入框获得焦点 → 用户修改值
  ├─ 输入框失焦 → handleBlur 触发 → validateField 单字段验证
  │     ├─ 验证通过 → formErrors 中移除该字段
  │     └─ 验证失败 → formErrors 中添加该字段错误信息
  └─ 表单提交 → validateForm 全量验证
        ├─ 存在错误 → 阻止提交，显示所有错误
        └─ 无错误 → 调用 login()
```

**提交流程：**
```
idle → validating → submitting → (success | error)
  idle: 初始状态，表单可操作
  validating: validateForm 执行
  submitting: isLoading=true，所有控件禁用
  success: navigate(ROUTES.HOME)
  error: error 写入 Store，表单恢复可操作
```

**组件卸载：**
```
卸载时 → clearEffect 执行 → clearError() 清除 Store 中的错误状态
```

### 核心处理逻辑

1. **字段级验证（validateField）**：根据字段名执行对应验证规则 — 用户名检查去除空格后是否为空，密码检查是否为空字符串。返回错误文案或 undefined
2. **失焦验证（handleBlur）**：在输入框失焦时调用 validateField，更新 formErrors 中对应字段。验证通过则删除该字段错误，失败则写入错误信息
3. **全量验证（validateForm）**：提交时对所有字段执行 validateField，收集所有错误并通过 setFormErrors 一次性更新。返回布尔值表示是否全部通过
4. **已认证跳转**：通过 useEffect 监听 isAuthenticated，变为 true 时自动 navigate 至首页
5. **错误清理**：通过 useEffect 的 cleanup 函数，在组件卸载时调用 clearError，防止残留错误在其他页面显示

## 结构说明

### Props 接口

LoginPage 为路由页面级组件，不接受外部 Props。

| 属性名 | 类型 | 必填 | 默认值 | 说明 |
|--------|------|------|--------|------|
| — | — | — | — | 无 Props，通过 useAuthStore 和 useNavigate 获取所需数据和方法 |

### 状态（State）

| 状态名 | 类型 | 初始值 | 说明 |
|--------|------|--------|------|
| username | `string` | `''` | 用户名输入值 |
| password | `string` | `''` | 密码输入值 |
| formErrors | `FormErrors`（`{ username?: string, password?: string }`） | `{}` | 字段级验证错误映射 |

**外部 Store 状态（useAuthStore）：**

| 状态/方法 | 类型 | 说明 |
|-----------|------|------|
| login | `(username: string, password: string) => Promise<void>` | 发起登录认证请求 |
| isLoading | `boolean` | 是否正在执行登录请求 |
| error | `string \| null` | 服务端返回的错误信息 |
| isAuthenticated | `boolean` | 用户是否已认证 |
| clearError | `() => void` | 清除 Store 中的错误状态 |

**路由依赖：**

| 依赖 | 说明 |
|------|------|
| useNavigate | 用于登录成功后和已认证用户的页面跳转 |
| ROUTES.HOME | 首页路由路径 |
| ROUTES.REGISTER | 注册页路由路径 |

### 主题变量依赖

| Tailwind 语义化 Class | 使用位置 | 说明 |
|------------------------|----------|------|
| `bg-background` | 页面根容器 | 页面背景色 |
| `text-foreground` | 页面标题、输入标签 | 主前景文字色 |
| `text-muted-foreground` | 副标题文案、底部注册引导文案 | 辅助/弱化文字色 |
| `text-primary` | "注册" 链接 | 主色调强调色（用于链接） |
| `bg-destructive/10` | 服务端错误提示卡片背景 | 错误色半透明背景 |
| `text-destructive` | 错误提示文字、必填星号、字段验证错误文案、错误输入框边框 | 错误/危险色 |
| `border-destructive` | 验证失败的输入框边框 | 错误边框色 |

### 子组件依赖

| 子组件 | 路径 | 说明 |
|--------|------|------|
| Button | `../../components/ui/button` | 基础 UI 按钮，用于登录提交按钮 |
| Input | `../../components/ui/input` | 基础 UI 输入框，用于用户名和密码输入 |

### 对外接口

| 接口 | 类型 | 说明 |
|------|------|------|
| `LoginPage` | 命名导出组件 | 登录页面组件，通过 `export function` 导出 |
| `default` | 默认导出 | 同时提供默认导出，兼容 `import LoginPage from '...'` 用法 |
| `FormErrors` | TypeScript Interface | 表单错误类型（模块内部类型，未导出） |
