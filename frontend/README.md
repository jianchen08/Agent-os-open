# 前端UI系统

基于React + TypeScript的Agent工作流系统前端应用。

## 技术栈

- **框架**: React 19 + TypeScript
- **构建工具**: Vite (Rolldown)
- **UI框架**: Tailwind CSS + Shadcn/ui
- **状态管理**: Zustand
- **路由**: React Router v6
- **图可视化**: React Flow
- **代码质量**: ESLint + Prettier

## 项目结构

```
frontend/
├── public/                 # 静态资源
├── src/
│   ├── assets/            # 图片、字体等资源
│   ├── components/        # 可复用组件
│   │   ├── ui/           # Shadcn/ui基础组件
│   │   ├── layout/       # 布局组件
│   │   ├── chat/         # 对话相关组件
│   │   ├── graph/        # 执行图组件
│   │   └── approval/     # 审批组件
│   ├── pages/            # 页面组件
│   ├── stores/           # Zustand状态管理
│   ├── services/         # 服务层
│   │   ├── api/         # API服务
│   │   ├── websocket/   # WebSocket服务
│   │   ├── mock/        # Mock数据服务
│   │   └── storage/     # 本地存储服务
│   ├── types/           # TypeScript类型定义
│   ├── utils/           # 工具函数
│   ├── hooks/           # 自定义Hooks
│   ├── constants/       # 常量定义
│   ├── lib/             # 库函数
│   ├── App.tsx          # 应用根组件
│   └── main.tsx         # 应用入口
├── .eslintrc.cjs        # ESLint配置
├── .prettierrc          # Prettier配置
├── tailwind.config.js   # Tailwind配置
├── tsconfig.json        # TypeScript配置
├── vite.config.ts       # Vite配置
└── package.json         # 项目依赖
```

## 开发指南

### 安装依赖

```bash
npm install
```

### 启动开发服务器

```bash
npm run dev
```

应用将在 http://localhost:3000 启动。

### 构建生产版本

```bash
npm run build
```

### 代码检查

```bash
# 运行ESLint
npm run lint

# 自动修复ESLint问题
npm run lint:fix

# 检查代码格式
npm run format:check

# 格式化代码
npm run format
```

### 预览生产构建

```bash
npm run preview
```

## 路径别名

项目配置了以下路径别名：

- `@/*` - src目录
- `@/components/*` - 组件目录
- `@/pages/*` - 页面目录
- `@/stores/*` - 状态管理目录
- `@/services/*` - 服务层目录
- `@/types/*` - 类型定义目录
- `@/utils/*` - 工具函数目录
- `@/hooks/*` - 自定义Hooks目录
- `@/constants/*` - 常量目录
- `@/assets/*` - 资源目录

## 开发规范

### 代码风格

- 使用TypeScript严格模式
- 遵循ESLint规则
- 使用Prettier格式化代码
- 组件使用函数式组件 + Hooks
- 使用Tailwind CSS进行样式开发

### 命名规范

- 组件文件：PascalCase (例如: `Button.tsx`)
- 工具函数：camelCase (例如: `formatDate.ts`)
- 常量：UPPER_SNAKE_CASE (例如: `API_BASE_URL`)
- 类型/接口：PascalCase (例如: `User`, `ApiResponse`)

### Git提交规范

- feat: 新功能
- fix: 修复bug
- docs: 文档更新
- style: 代码格式调整
- refactor: 重构
- test: 测试相关
- chore: 构建/工具链相关

## 环境变量

创建 `.env.local` 文件配置环境变量：

```env
VITE_API_BASE_URL=http://localhost:8000
VITE_WS_URL=ws://localhost:8000/ws
```

## 浏览器支持

- Chrome (最新版)
- Firefox (最新版)
- Safari (最新版)
- Edge (最新版)

## 许可证

MIT
