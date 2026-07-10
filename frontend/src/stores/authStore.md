# useAuthStore

## 用途

Zustand 认证状态管理 Store，使用真实后端 API 进行登录、注册、登出和令牌管理。

## API

### 状态

| 属性 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `user` | `User \| null` | `null` | 当前登录用户信息 |
| `token` | `string \| null` | `null` | JWT 访问令牌 |
| `refreshTokenValue` | `string \| null` | `null` | JWT 刷新令牌 |
| `isAuthenticated` | `boolean` | `false` | 是否已认证 |
| `isLoading` | `boolean` | `false` | 是否正在执行认证操作 |
| `isInitializing` | `boolean` | `true` | 是否正在初始化认证状态 |
| `error` | `string \| null` | `null` | 最近一次操作的错误信息 |

### 方法

#### `login(username: string, password: string): Promise<void>`

用户登录。调用 `POST /api/v1/auth/login`，成功后存储 token 到 localStorage 并获取用户信息。

- **输入验证**：用户名和密码不能为空
- **Token 存储**：access_token、refresh_token、expiry time 写入 localStorage
- **用户信息**：登录后自动调用 `fetchCurrentUser()`
- **降级处理**：获取用户信息失败时使用基本用户信息

#### `register(username: string, password: string, email: string): Promise<void>`

用户注册。调用 `POST /api/v1/auth/register`，注册成功后自动登录。

- 注册后自动获取 token 和用户信息（流程同 login）

#### `logout(): Promise<void>`

用户登出。调用 `POST /api/v1/auth/logout`，清除 localStorage 和 Store 状态。

- 即使后端登出 API 失败，仍会清除本地状态

#### `refreshToken(): Promise<void>`

刷新访问令牌。调用 `POST /api/v1/auth/refresh`。

- 刷新失败时自动调用 `logout()` 清除认证状态

#### `initializeAuth(): Promise<void>`

初始化认证状态。从 localStorage 恢复 token，检查是否过期。

- **Token 有效**：恢复认证状态，异步获取最新用户信息
- **Token 过期**：尝试用 refresh_token 刷新
- **无 Token**：跳过初始化

#### `checkTokenExpiration(): boolean`

检查当前 token 是否已过期。返回 `true` 表示已过期。

#### `fetchCurrentUser(): Promise<void>`

获取当前用户信息。调用 `GET /api/v1/auth/me`，结果写入 Store 和 localStorage。

#### `clearError(): void`

清除 error 状态。

## 使用示例

```tsx
import { useAuthStore } from '@/stores/authStore'

function LoginPage() {
  const { login, isLoading, error, isAuthenticated, clearError } = useAuthStore()

  const handleSubmit = async (username: string, password: string) => {
    try {
      await login(username, password)
    } catch (err) {
      // error 已存储在 store 中
    }
  }

  // ...
}
```

## 依赖关系

| 依赖 | 类型 | 说明 |
|------|------|------|
| `zustand` | 状态管理库 | Store 创建工具 |
| `authApi` | API 服务 | 后端认证 API 调用 |
| `STORAGE_KEYS` | 常量 | localStorage 键名 |

### localStorage 键（STORAGE_KEYS）

| 键 | 说明 |
|----|------|
| `ACCESS_TOKEN` | 访问令牌 |
| `REFRESH_TOKEN` | 刷新令牌 |
| `ACCESS_TOKEN_EXPIRY` | Token 过期时间戳 |
| `AUTH_USER` | 用户信息 JSON |

## 注意事项

1. **初始化流程**：应用启动时需调用 `initializeAuth()` 恢复登录状态
2. **Token 自动刷新**：`initializeAuth` 中检测到 token 过期会自动尝试刷新
3. **安全降级**：获取用户信息失败不阻断登录流程，使用基本用户信息替代
4. **不使用 persist 中间件**：认证状态通过手动 localStorage 管理而非 zustand/persist
