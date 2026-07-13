# useThemeStore

## 用途

Zustand + persist 主题状态管理 Store。管理 light/dark/system 主题模式，支持预设主题和用户自定义主题，负责将主题配置应用到 DOM。

## API

### 状态

| 属性 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `mode` | `ThemeMode` | `'dark'` | 主题模式（light/dark/system） |
| `currentThemeId` | `string` | `'dark'` | 当前主题 ID |
| `resolvedTheme` | `'light' \| 'dark'` | `'dark'` | 实际应用的主题（system 模式解析后） |
| `themeConfig` | `ThemeConfig \| null` | `null` | 当前加载的主题配置 |
| `availableThemes` | `ThemeInfo[]` | `[]` | 可用主题列表（预设 + 用户自定义） |
| `isLoading` | `boolean` | `false` | 是否正在加载主题 |

### 方法

#### `setMode(mode: ThemeMode): void`

设置主题模式。若 mode 为 `system`，自动解析系统偏好。

- 如果模式变更导致主题 ID 变化，自动调用 `loadTheme()`

#### `setTheme(themeId: string): Promise<void>`

切换到指定主题。同时更新 mode 和 currentThemeId，然后加载主题配置。

#### `loadTheme(themeId: string): Promise<void>`

加载主题配置。按优先级查找：

1. **预设主题**：从 `getPresetTheme()` 加载
2. **用户自定义主题**：从 `ThemeStorageService` 加载，合并基础主题配置
3. **回退**：加载失败时回退到 dark 主题

#### `loadUserThemes(): void`

加载用户自定义主题。内部调用 `updateAvailableThemes()` 合并列表。

#### `applyTheme(): void`

将当前 `themeConfig` 应用到 DOM。

- 调用 `applyThemeToDOM()` 批量设置 CSS 变量
- 处理背景图片（image）相关 CSS 变量
- 处理纹理（texture：dots/grid/lines/noise）CSS 变量

#### `resetTheme(): void`

重置为 dark 默认主题。

#### `updateAvailableThemes(): void`

更新可用主题列表，合并预设主题和用户自定义主题。

#### `refreshThemes(): void`

刷新主题列表。调用 `updateAvailableThemes()`。

## 使用示例

```tsx
import { useThemeStore } from '@/stores/themeStore'

function ThemePanel() {
  const { mode, setMode, setTheme, availableThemes, isLoading } = useThemeStore()

  return (
    <div>
      <select value={mode} onChange={(e) => setMode(e.target.value as ThemeMode)}>
        <option value="light">浅色</option>
        <option value="dark">深色</option>
        <option value="system">跟随系统</option>
      </select>
      {availableThemes.map((theme) => (
        <button key={theme.id} onClick={() => setTheme(theme.id)}>
          {theme.name}
        </button>
      ))}
    </div>
  )
}
```

## 依赖关系

| 依赖 | 类型 | 说明 |
|------|------|------|
| `zustand` + `persist` | 状态管理 | persist 持久化 mode 和 currentThemeId |
| `themeList` | 配置 | 预设主题列表 |
| `getPresetTheme` | 服务函数 | 从预设加载 ThemeConfig |
| `applyThemeToDOM` | 服务函数 | 将 ThemeConfig 批量写入 DOM CSS 变量 |
| `ThemeStorageService` | 服务 | 用户自定义主题的增删改查 |
| `mergeTheme` | 工具函数 | 合并基础主题和用户自定义配置 |

### 持久化策略

使用 `zustand/persist`，仅持久化 `mode` 和 `currentThemeId` 到 `theme-storage` 键。

### 纹理类型

| 纹理 | 生成方式 |
|------|----------|
| `dots` | `radial-gradient` 点阵 |
| `grid` | 双向 `linear-gradient` 网格 |
| `lines` | `repeating-linear-gradient` 水平线 |
| `noise` | SVG `feTurbulence` 滤镜 |

## 注意事项

1. **初始化**：应用启动时需调用 `initializeTheme()` 全局函数（非 Store 方法）
2. **system 模式**：监听 `prefers-color-scheme` 媒体查询变化，自动切换
3. **用户主题合并**：用户自定义主题基于某个预设主题，通过 `mergeTheme` 合并
4. **背景图片**：启用时给 body 添加 `has-bg-image` class，通过 CSS 变量控制
5. **isLoading 保护**：主题加载期间 `isLoading=true`，可在 UI 层显示加载状态
