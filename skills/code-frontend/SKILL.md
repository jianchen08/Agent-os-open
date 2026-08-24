---
name: 前端编码
description: 前端技术栈编码技能（规范+流程）。含组件规范、样式规范、状态管理、API 调用、设计系统一致性、前端 E2E 测试要求。用于前端/全栈任务的编码阶段。
---

# 前端编码

## 组件规范

### 组件命名

| 类型 | 规范 | 示例 |
|------|------|------|
| 组件文件 | PascalCase | `UserProfile.vue`, `LoginForm.tsx` |
| 组件名 | PascalCase | `UserProfile`, `OrderList` |
| 组件文件夹 | kebab-case | `user-profile/`, `order-list/` |
| Props | camelCase | `userName`, `isLoading` |
| 事件名 | camelCase（以 on 开头） | `onClick`, `onChange` |
| 方法名 | camelCase | `handleSubmit`, `fetchData` |

### 组件设计原则

| 原则 | 说明 | 优先级 |
|------|------|--------|
| 受控组件 | 表单值受 state 控制 | 高 |
| Props 校验 | 使用 TypeScript 或 PropTypes | 高 |
| 组件拆分阈值 | 超过 150 行考虑拆分 | 中 |

### React 组件结构

```tsx
// 1. 类型定义 → 2. 组件定义 → 3. Hooks → 4. 业务逻辑 → 5. 渲染 → 6. 导出
interface UserCardProps {
  userId: number;
  userName: string;
  avatar?: string;
}

function UserCard({ userId, userName, avatar }: UserCardProps) {
  const [isEditing, setIsEditing] = useState(false);
  const handleEdit = () => setIsEditing(true);

  return (
    <div className={styles.card}>
      <img src={avatar} alt={userName} />
      <span>{userName}</span>
      <button onClick={handleEdit}>编辑</button>
    </div>
  );
}

export default UserCard;
```

### Vue 组件结构

```vue
<template>
  <div class="user-card">
    <img :src="avatar" :alt="userName" />
    <span>{{ userName }}</span>
    <button @click="handleEdit">编辑</button>
  </div>
</template>

<script setup lang="ts">
interface Props { userId: number; userName: string; avatar?: string; }
const props = defineProps<Props>();
const emit = defineEmits<{ (e: 'edit', userId: number): void; }>();
const handleEdit = () => emit('edit', props.userId);
</script>

<style scoped>
.user-card { display: flex; align-items: center; }
</style>
```

## 设计系统一致性（编码前必做）

编写前端代码前必须先执行发现步骤，不能跳过：

1. **识别设计系统来源**：查 package.json 的组件库依赖、设计令牌文件（theme.css/tokens.json/tailwind.config）、全局样式文件、已有同类组件实现
2. **提取设计规范**：间距梯度、颜色语义变量、字体梯度、可复用组件、布局断点
3. **以此为锚点**：所有样式决策必须以提取到的规范为基准

### 间距
- 先从项目的设计令牌/全局样式找到间距梯度
- 所有间距值必须从项目定义的梯度中选取
- 项目未定义时，使用项目中最常见的间距倍数作为基准

### 颜色
- 先找到项目的语义色变量（主色、成功色、警告色、错误色）
- 所有颜色通过项目的变量/token 引用
- 需新颜色时在设计令牌中新增变量后引用，禁止硬编码

### 字体
- 遵循项目已有的字体梯度，不引入新的字号或字重值

### 新增设计令牌流程
1. 在设计令牌文件中新增变量 → 2. 在组件中通过变量引用 → 3. 注释说明新增原因

## 样式规范

### BEM 命名法

| 类型 | 命名规则 | 示例 |
|------|---------|------|
| Block | 语义化名词，kebab-case | `card`, `user-profile` |
| Element | `block__element` | `card__title`, `user-profile__name` |
| Modifier | `--modifier` | `card--highlighted`, `card__title--large` |

### 样式要点
- 使用 CSS Modules，避免全局样式污染
- 避免内联样式（除动态计算值外禁止）
- 使用语义化类名（BEM）
- 嵌套最多 3 层
- 提取公共样式使用 CSS 变量或 mixins

## 状态管理

### 状态分层

| 层级 | 管理方式 | 示例 |
|------|---------|------|
| 组件级 | useState/useReducer / ref/reactive | 当前展开状态、本地表单值 |
| 页面级 | useState / ref/reactive | 当前页码、搜索词 |
| 应用级 | Redux/Zustand/Pinia | 用户登录态、主题设置 |
| 服务级 | React Query/SWR | 接口数据缓存 |

### Redux Slice 定义

```typescript
import { createSlice, PayloadAction } from '@reduxjs/toolkit';

const userSlice = createSlice({
  name: 'user',
  initialState: { id: null, name: '', isLoading: false },
  reducers: {
    setUser: (state, action: PayloadAction<User>) => { state.id = action.payload.id; },
    clearUser: (state) => { state.id = null; },
  },
});
export const { setUser, clearUser } = userSlice.actions;
```

### Zustand Store

```typescript
import { create } from 'zustand';

const useUserStore = create<UserState>((set) => ({
  id: null, name: '',
  setUser: (user) => set({ id: user.id, name: user.name }),
  clearUser: () => set({ id: null, name: '' }),
}));
```

## API 调用约定

- 通过封装层调用（API 模块封装），不直接在组件里 fetch

```typescript
// API 模块封装
export const userApi = {
  list: (params: ListParams) => apiClient.get<User[]>('/users', { params }),
  get: (id: number) => apiClient.get<User>(`/users/${id}`),
  create: (data: CreateUserData) => apiClient.post<User>('/users', data),
  update: (id: number, data: UpdateUserData) => apiClient.patch<User>(`/users/${id}`, data),
  delete: (id: number) => apiClient.delete(`/users/${id}`),
};
```

- 统一响应格式：成功 `{"success": true, "data": {}, "message": "操作成功"}`，错误 `{"success": false, "error": {"code": "USER_NOT_FOUND", "message": "用户不存在"}}`

| 错误类型 | 处理方式 | 用户提示 |
|---------|---------|---------|
| 401 未认证 | 跳转登录页 | "请先登录" |
| 403 无权限 | 显示无权限提示 | "您没有权限执行此操作" |
| 404 未找到 | 显示 404 页面 | "资源不存在" |
| 500 服务器错误 | 显示错误页 | "服务器错误，请稍后重试" |
| 网络错误 | 显示重试提示 | "网络连接失败，点击重试" |

## 前端测试要求

进入测试阶段时（测试通用规范已在常驻提示词）：

- **UI 功能类任务**：必须用 playwright_test 做浏览器级测试，不能只用 Jest 组件测试代替
- **渲染/显示类任务**：验证阶段包含视觉验证（截图对比）
- **交互类任务**（按钮点击、表单提交、页面跳转）：在真实浏览器环境验证交互流程

## 禁止行为

### 组件开发
- 内联样式（除动态值）→ CSS 类名或 CSS Modules
- 直接操作 DOM → 使用 React/Vue API
- 大文件组件（>500行）→ 拆分为小组件
- 魔法数字/字符串 → 定义常量
- 未处理的 Promise → async/await + try-catch

### 状态管理
- 组件内直接 fetch → 使用 API 层封装
- 滥用全局状态 → 使用 Props 或 Context
- 渲染中执行副作用 → 使用 useEffect
- 缺少依赖的 useEffect → 添加完整依赖数组

### 样式
- `!important` → 使用更高优先级选择器
- ID 选择器 → class 选择器
- 标签选择器滥用 → class 选择器
- 固定宽度（响应式场景）→ 相对单位或 Flex/Grid

### UI/UX 质量
- 语义化标记：`<button>` 而非 `<div onclick>`，`<nav>` 定义导航，`<main>` 定义主内容，`<article>`/`<section>`/`<aside>` 划分内容结构，`<form>`/`<label>`/`<input>` 语义标签
- 可访问性、性能优化、XSS 防护的审查维度见「审查清单七大维度」
