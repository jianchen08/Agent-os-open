# 前端开发规范

---

## 概述

本规范定义了灵汐系统前端开发的标准规范和最佳实践，涵盖组件设计、样式管理、状态管理、API调用和测试规范。

### 来源标识说明

| 标识 | 含义 |
|------|------|
| [DOC] | 来自官方文档的推荐做法 |
| [BEST] | 社区公认的最佳实践 |
| [STD] | 来自国际标准、行业标准 |
| [TEAM] | 团队内部约定的规范 |
| [RESEARCH] | 基于系统调研的结论 |

---

## 1. 组件规范

### 1.1 组件命名规范

来源：[BEST] React/Vue 社区最佳实践

| 类型 | 规范 | 示例 |
|------|------|------|
| 组件文件 | PascalCase | `UserProfile.vue`, `LoginForm.tsx` |
| 组件名 | PascalCase | `UserProfile`, `OrderList` |
| 组件文件夹 | kebab-case | `user-profile/`, `order-list/` |
| Props | camelCase | `userName`, `isLoading` |
| 事件名 | camelCase（以 on 开头） | `onClick`, `onChange` |
| 方法名 | camelCase | `handleSubmit`, `fetchData` |

### 1.2 React 组件结构

来源：[DOC] React 官方文档

```tsx
// 1. 类型定义（Props、State、Refs）
interface UserCardProps {
  userId: number;
  userName: string;
  avatar?: string;
}

interface UserCardState {
  isLoading: boolean;
  error: string | null;
}

// 2. 组件定义
function UserCard({ userId, userName, avatar }: UserCardProps) {
  // 3. Hooks（useState, useEffect 等）
  const [isEditing, setIsEditing] = useState(false);
  
  // 4. 业务逻辑
  const handleEdit = () => {
    setIsEditing(true);
  };
  
  // 5. 渲染
  return (
    <div className={styles.card}>
      <img src={avatar} alt={userName} />
      <span>{userName}</span>
      <button onClick={handleEdit}>编辑</button>
    </div>
  );
}

// 6. 导出
export default UserCard;
```

### 1.3 Vue 组件结构

来源：[DOC] Vue 官方文档

```vue
<template>
  <!-- 模板结构 -->
  <div class="user-card">
    <img :src="avatar" :alt="userName" />
    <span>{{ userName }}</span>
    <button @click="handleEdit">编辑</button>
  </div>
</template>

<script setup lang="ts">
// 1. 类型定义
interface Props {
  userId: number;
  userName: string;
  avatar?: string;
}

// 2. Props 定义
const props = defineProps<Props>();

// 3. Emits 定义
const emit = defineEmits<{
  (e: 'edit', userId: number): void;
}>();

// 4. 业务逻辑
const handleEdit = () => {
  emit('edit', props.userId);
};
</script>

<style scoped>
/* 5. 样式定义（使用 scoped） */
.user-card {
  display: flex;
  align-items: center;
}
</style>
```

### 1.4 组件设计原则

来源：[BEST] React/Vue 社区最佳实践

| 原则 | 说明 | 优先级 |
|------|------|--------|
| 单一职责 | 组件只做一件事，职责单一 | 高 |
| 纯展示组件 | 无状态，通过 props 接收数据 | 中 |
| 容器组件 | 管理状态和业务逻辑 | 中 |
| 受控组件 | 表单值受 state 控制 | 高 |
| 组件拆分阈值 | 超过 150 行考虑拆分 | 中 |
| Props 校验 | 使用 TypeScript 或 PropTypes | 高 |

---

## 2. 样式规范

### 2.1 BEM 命名法

来源：[BEST] BEM 方法论（https://bem.info）

BEM（Block Element Modifier）是一种命名约定，使 CSS 类名语义化、结构化。

```css
/* Block: 独立的页面组件 */
.card { }

/* Element: Block 的子元素 */
.card__title { }
.card__body { }
.card__image { }

/* Modifier: Block 或 Element 的变体 */
.card--highlighted { }
.card__title--large { }
.card--disabled { }
```

**命名规则**：
| 类型 | 命名规则 | 示例 |
|------|---------|------|
| Block | 语义化名词，kebab-case | `card`, `user-profile` |
| Element | `block__element`，kebab-case | `card__title`, `user-profile__name` |
| Modifier | `--modifier`，kebab-case | `card--highlighted`, `card__title--large` |

### 2.2 CSS Modules

来源：[DOC] CSS Modules 官方文档

CSS Modules 提供局部作用域，避免样式冲突。

```css
/* Card.module.css */
.title {
  font-size: 18px;
  font-weight: bold;
}

.body {
  padding: 16px;
}

.highlighted {
  background-color: #fff3cd;
}
```

```tsx
// React 使用
import styles from './Card.module.css';

function Card({ title, body, highlighted }) {
  return (
    <div className={styles.title}>
      <h2 className={highlighted ? styles.highlighted : ''}>{title}</h2>
      <p className={styles.body}>{body}</p>
    </div>
  );
}
```

### 2.3 样式规范要点

来源：[BEST] 前端样式最佳实践

| 规范 | 说明 | 来源 |
|------|------|------|
| 使用 CSS Modules | 避免全局样式污染 | [BEST] |
| 避免内联样式 | 除动态计算值外禁止 | [BEST] |
| 使用语义化类名 | BEM 命名法 | [BEST] |
| 避免嵌套过深 | 最多 3 层 | [BEST] |
| 提取公共样式 | 使用 CSS 变量或 mixins | [BEST] |

---

## 3. 状态管理

### 3.1 状态管理方案选型

来源：[BEST] 状态管理最佳实践

| 方案 | 适用场景 | 核心概念 | 来源 |
|------|---------|---------|------|
| **Redux** | 复杂中大型应用 | Store、Action、Reducer、Selector | [DOC] Redux 官方文档 |
| **Zustand** | 轻量级应用 | Store、Action（更简洁） | [BEST] 社区推荐 |
| **Vuex/Pinia** | Vue 应用 | State、Getter、Mutation/Action | [DOC] 官方文档 |
| **React Context** | 简单共享状态 | Provider、Consumer | [DOC] React 官方文档 |
| **React Query/SWR** | 服务端状态缓存 | Query、Mutation、Cache | [DOC] 官方文档 |

### 3.2 状态分层原则

来源：[BEST] 状态管理最佳实践

| 层级 | 状态类型 | 管理方式 | 示例 |
|------|---------|---------|------|
| 组件级 | 组件私有状态 | React: useState/useReducer<br>Vue: ref/reactive | 当前展开状态、本地表单值 |
| 页面级 | 页面数据、URL 状态 | React: useState<br>Vue: ref/reactive | 当前页码、搜索词、筛选条件 |
| 应用级 | 跨页共享状态 | Redux/Zustand/Vuex/Pinia | 用户登录态、购物车、主题设置 |
| 服务级 | API 缓存 | React Query/SWR | 接口数据缓存、重试机制 |

### 3.3 Redux 最佳实践

来源：[DOC] Redux 官方文档

```typescript
// 1. 目录结构
src/
├── features/
│   └── user/
│       ├── userSlice.ts      // slice 定义
│       ├── userSelectors.ts  // selectors
│       └── user.types.ts     // 类型定义
└── store/
    └── index.ts               // store 配置

// 2. Slice 定义
import { createSlice, PayloadAction } from '@reduxjs/toolkit';

interface UserState {
  id: number | null;
  name: string;
  email: string;
  isLoading: boolean;
}

const initialState: UserState = {
  id: null,
  name: '',
  email: '',
  isLoading: false,
};

const userSlice = createSlice({
  name: 'user',
  initialState,
  reducers: {
    setUser: (state, action: PayloadAction<User>) => {
      state.id = action.payload.id;
      state.name = action.payload.name;
      state.email = action.payload.email;
    },
    clearUser: (state) => {
      state.id = null;
      state.name = '';
      state.email = '';
    },
  },
});

export const { setUser, clearUser } = userSlice.actions;
export default userSlice.reducer;
```

### 3.4 Zustand 最佳实践

来源：[BEST] 社区推荐

```typescript
import { create } from 'zustand';

interface UserState {
  id: number | null;
  name: string;
  setUser: (user: User) => void;
  clearUser: () => void;
}

const useUserStore = create<UserState>((set) => ({
  id: null,
  name: '',
  setUser: (user) => set({ id: user.id, name: user.name }),
  clearUser: () => set({ id: null, name: '' }),
}));

// 使用
function UserProfile() {
  const { name, setUser } = useUserStore();
  // ...
}
```

---

## 4. API 调用约定

### 4.1 RESTful 设计原则

来源：[STD] RFC 7231

| 方法 | 用途 | 示例 | 响应状态码 |
|------|------|------|-----------|
| GET | 查询资源 | `GET /users/123` | 200 |
| POST | 创建资源 | `POST /users` | 201 |
| PUT | 更新资源（全量） | `PUT /users/123` | 200 |
| PATCH | 更新资源（部分） | `PATCH /users/123` | 200 |
| DELETE | 删除资源 | `DELETE /users/123` | 204 |

### 4.2 API 封装层

来源：[BEST] API 封装最佳实践

```typescript
// 1. API 客户端封装
class ApiClient {
  private baseURL: string;
  
  constructor(baseURL: string) {
    this.baseURL = baseURL;
  }
  
  async get<T>(path: string): Promise<T> {
    const response = await fetch(`${this.baseURL}${path}`);
    if (!response.ok) {
      throw new ApiError(response.status, await response.json());
    }
    return response.json();
  }
  
  async post<T>(path: string, data: unknown): Promise<T> {
    const response = await fetch(`${this.baseURL}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    if (!response.ok) {
      throw new ApiError(response.status, await response.json());
    }
    return response.json();
  }
}

// 2. API 模块封装
export const userApi = {
  list: (params: ListParams) => apiClient.get<User[]>('/users', { params }),
  get: (id: number) => apiClient.get<User>(`/users/${id}`),
  create: (data: CreateUserData) => apiClient.post<User>('/users', data),
  update: (id: number, data: UpdateUserData) => apiClient.patch<User>(`/users/${id}`, data),
  delete: (id: number) => apiClient.delete(`/users/${id}`),
};
```

### 4.3 统一响应格式

来源：[BEST] RESTful API 最佳实践

**成功响应**：
```json
{
  "success": true,
  "data": { },
  "message": "操作成功",
  "code": 200
}
```

**错误响应**：
```json
{
  "success": false,
  "error": {
    "code": "USER_NOT_FOUND",
    "message": "用户不存在",
    "details": { }
  }
}
```

### 4.4 API 错误处理

来源：[TEAM] 团队约定

| 错误类型 | 处理方式 | 用户提示 |
|---------|---------|---------|
| 401 未认证 | 跳转登录页 | "请先登录" |
| 403 无权限 | 显示无权限提示 | "您没有权限执行此操作" |
| 404 未找到 | 显示 404 页面 | "资源不存在" |
| 500 服务器错误 | 显示错误页 | "服务器错误，请稍后重试" |
| 网络错误 | 显示重试提示 | "网络连接失败，点击重试" |

---

## 5. 前端交互测试

### 5.1 测试工具选型

来源：[RESEARCH] 系统调研结论 + [TOOL] Playwright/Vitest 官方文档

| 工具 | 用途 | 协议 | Agent 可编程性 | 来源 |
|------|------|------|-------------|------|
| **Playwright** | E2E 测试 | MIT | 优秀 | [TOOL] 官方文档 |
| **Vitest** | 单元/组件测试 | MIT | 优秀 | [TOOL] 官方文档 |
| **Testing Library** | 组件测试 | MIT | 优秀 | [TOOL] 官方文档 |

**推荐方案**：Playwright（E2E）+ Vitest + Testing Library（组件）

### 5.2 测试编写规范

来源：[BEST] Playwright 官方文档

| 规范 | 说明 | 来源 |
|------|------|------|
| 页面对象模式 | 封装页面元素和操作，提高可维护性 | [BEST] |
| 数据测试属性 | 使用 `data-testid` 而非 CSS 选择器 | [BEST] |
| 异步等待 | 使用 `waitFor` 替代固定 `sleep` | [BEST] |
| 测试隔离 | 每个测试独立，不依赖执行顺序 | [BEST] |

### 5.3 页面对象模式示例

来源：[BEST] Playwright 最佳实践

```typescript
// pages/LoginPage.ts
export class LoginPage {
  constructor(private page: Page) {}
  
  // 页面元素定位器
  get usernameInput() {
    return this.page.getByTestId('username-input');
  }
  
  get passwordInput() {
    return this.page.getByTestId('password-input');
  }
  
  get submitButton() {
    return this.page.getByTestId('login-submit');
  }
  
  get errorMessage() {
    return this.page.getByTestId('error-message');
  }
  
  // 页面操作
  async login(username: string, password: string) {
    await this.usernameInput.fill(username);
    await this.passwordInput.fill(password);
    await this.submitButton.click();
  }
  
  // 断言方法
  async expectErrorVisible() {
    await expect(this.errorMessage).toBeVisible();
  }
}

// 测试用例
test('登录失败显示错误提示', async ({ page }) => {
  const loginPage = new LoginPage(page);
  await loginPage.goto();
  await loginPage.login('invalid', 'wrong');
  await loginPage.expectErrorVisible();
});
```

### 5.4 Vitest 组件测试示例

来源：[DOC] Vitest 官方文档

```typescript
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { LoginForm } from './LoginForm';

describe('LoginForm', () => {
  it('提交表单时调用 onSubmit', async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    
    render(<LoginForm onSubmit={onSubmit} />);
    
    await user.type(screen.getByTestId('username-input'), 'testuser');
    await user.type(screen.getByTestId('password-input'), 'password123');
    await user.click(screen.getByTestId('submit-button'));
    
    expect(onSubmit).toHaveBeenCalledWith({
      username: 'testuser',
      password: 'password123',
    });
  });
});
```

---

## 6. 禁止行为

### 6.1 组件开发禁止行为

来源：[BEST] 前端开发反模式

| 禁止行为 | 风险 | 替代方案 | 来源 |
|----------|------|----------|------|
| 禁止内联样式（除动态值） | 难以维护和覆盖 | CSS 类名或 CSS Modules | [BEST] |
| 禁止直接操作 DOM | 破坏虚拟 DOM 一致性 | 使用 React/Vue API | [BEST] |
| 禁止大文件组件（>500行） | 难以维护 | 拆分为小组件 | [TEAM] |
| 禁止魔法数字/字符串 | 难以理解 | 定义常量 | [BEST] |
| 禁止未处理的 Promise | 静默失败风险 | 使用 async/await + try-catch | [BEST] |
| 禁止 any 类型滥用 | 失去类型安全 | 使用具体类型或 unknown | [BEST] |

### 6.2 状态管理禁止行为

来源：[BEST] 状态管理反模式

| 禁止行为 | 风险 | 替代方案 | 来源 |
|----------|------|----------|------|
| 禁止在组件内直接 fetch | 难以复用和测试 | 使用 API 层封装 | [BEST] |
| 禁止滥用全局状态 | 状态难以追踪 | 使用 Props 或 Context | [BEST] |
| 禁止在渲染中执行副作用 | 性能问题 | 使用 useEffect | [BEST] |
| 禁止缺少依赖的 useEffect | 行为不可预期 | 添加完整依赖数组 | [BEST] |

### 6.3 样式禁止行为

来源：[BEST] CSS 最佳实践

| 禁止行为 | 风险 | 替代方案 | 来源 |
|----------|------|----------|------|
| 禁止使用 `!important` | 样式难以覆盖 | 使用更高优先级选择器 | [BEST] |
| 禁止 ID 选择器 | 样式难以复用 | 使用 class 选择器 | [BEST] |
| 禁止标签选择器滥用 | 样式污染 | 使用 class 选择器 | [BEST] |
| 禁止固定宽度（响应式场景） | 不适配不同屏幕 | 使用相对单位或 Flex/Grid | [BEST] |

---

## 7. UI/UX 质量

### 7.1 语义化标记

来源：[STD] W3C HTML 规范

| 规范 | 说明 | 来源 |
|------|------|------|
| 使用语义化标签 | `<button>` 而非 `<div onclick>`，`<nav>` 定义导航，`<main>` 定义主内容 | [STD] W3C |
| 内容结构划分 | 使用 `<article>`、`<section>`、`<aside>` 划分内容结构 | [STD] W3C |
| 表单语义化 | 使用 `<form>`、`<label>`、`<input>` 语义标签 | [STD] W3C |

### 7.2 可访问性

来源：[STD] WCAG 2.1

| 规范 | 说明 | 来源 |
|------|------|------|
| 键盘可操作 | 所有可点击元素可通过 Tab 键聚焦 | [STD] WCAG |
| 图像替代文本 | 图片必须提供 alt 文本描述 | [STD] WCAG |
| 表单标签关联 | 表单字段必须有关联的 label | [STD] WCAG |
| 图标按钮说明 | 使用 `aria-label` 为图标按钮提供说明 | [STD] WCAG |
| 颜色对比度 | 满足 WCAG 标准 | [STD] WCAG |

### 7.3 视觉一致性

来源：[BEST] 前端设计最佳实践

| 规范 | 说明 | 来源 |
|------|------|------|
| 使用设计系统 | 统一色板、字体、间距 | [BEST] |
| 一致的样式 | 相同类型元素使用一致的样式 | [BEST] |
| 遵循布局规范 | 遵循既定的布局规范和间距规则 | [BEST] |

---

## 8. 性能优化

### 8.1 渲染性能

来源：[DOC] React/Vue 官方文档

| 规范 | 说明 | 来源 |
|------|------|------|
| 精准更新 | 组件状态变更应精准触发更新 | [DOC] |
| 虚拟滚动 | 列表数据量大时使用虚拟滚动 | [BEST] |
| 避免渲染中创建 | 避免在渲染函数中创建新函数或对象 | [DOC] |
| Memo 优化 | 使用 React.memo/Vue.memo 等避免不必要重渲染 | [DOC] |

### 8.2 懒加载

来源：[BEST] 前端性能最佳实践

| 规范 | 说明 | 来源 |
|------|------|------|
| 动态导入 | 首屏不需要的资源使用动态导入 | [BEST] |
| 图片懒加载 | 使用 Intersection Observer | [BEST] |
| 路由按需加载 | 路由组件按需加载 | [BEST] |
| 第三方库按需引入 | 按需引入，避免全量打包 | [BEST] |

### 8.3 防抖节流

来源：[BEST] 前端性能最佳实践

| 规范 | 说明 | 来源 |
|------|------|------|
| 搜索输入防抖 | 使用 debounce | [BEST] |
| 滚动事件节流 | 使用 throttle | [BEST] |
| 窗口 resize 防抖 | 使用 debounce | [BEST] |
| 表单验证防抖 | 减少验证频率 | [BEST] |

---

## 9. XSS 防护

### 9.1 输入输出转义

来源：[STD] OWASP XSS 防护规范

| 规范 | 说明 | 来源 |
|------|------|------|
| 用户输入转义 | 用户输入内容必须经过转义处理后再渲染 | [STD] OWASP |
| 框架默认转义 | 使用框架提供的默认转义机制 | [BEST] |
| 禁止 dangerouslySetInnerHTML | 除非确有必要，禁止使用 | [DOC] |
| 富文本过滤 | 富文本使用专业过滤库处理 | [STD] OWASP |

### 9.2 CSP 策略

来源：[STD] W3C Content Security Policy

| 规范 | 说明 | 来源 |
|------|------|------|
| 配置严格 CSP | 配置严格的内容安全策略 | [STD] W3C |
| 禁止内联脚本 | 禁止内联脚本执行 | [STD] W3C |
| 限制资源来源 | 限制资源加载来源 | [STD] W3C |
