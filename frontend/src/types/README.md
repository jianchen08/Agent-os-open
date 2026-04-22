# 类型定义文档

本目录包含前端应用的所有 TypeScript 类型定义。

## 文件结构

```
types/
├── index.ts          # 统一导出所有类型
├── models.ts         # 核心数据模型类型
├── graph.ts          # 执行图相关类型
├── api.ts            # API 请求响应类型
└── __test__/         # 类型定义测试
    └── types.test.ts # 类型验证测试
```

## 类型分类

### 核心数据模型 (models.ts)

定义了应用的核心业务实体：

- **User**: 用户信息
- **Session**: 会话信息
- **Message**: 消息内容
- **MessageRole**: 消息角色类型 ('user' | 'assistant' | 'system')
- **ApprovalRequest**: 审批请求
- **RiskLevel**: 风险等级 ('low' | 'medium' | 'high')

### 执行图类型 (graph.ts)

定义了任务执行流程可视化相关的类型：

- **GraphData**: 执行图数据容器
- **Node**: 图节点
- **Edge**: 图边
- **NodeType**: 节点类型 ('task' | 'tool' | 'decision')
- **NodeStatus**: 节点状态 ('pending' | 'running' | 'completed' | 'failed')
- **NodePosition**: 节点位置坐标
- **NodeData**: 节点数据

### API 类型 (api.ts)

定义了与后端 API 交互的请求和响应类型：

#### 认证相关
- **AuthResponse**: 认证响应
- **TokenResponse**: 令牌响应
- **LoginRequest**: 登录请求
- **RegisterRequest**: 注册请求

#### 会话和消息相关
- **CreateSessionResponse**: 创建会话响应
- **GetSessionsResponse**: 获取会话列表响应
- **GetMessagesResponse**: 获取消息列表响应
- **SendMessageRequest**: 发送消息请求
- **SendMessageResponse**: 发送消息响应

#### 执行图相关
- **GetGraphResponse**: 获取执行图响应

#### 通用类型
- **ApiError**: API 错误响应
- **ApiResponse<T>**: 通用 API 响应包装器

## 使用示例

### 导入类型

```typescript
// 导入单个类型
import type { User, Session, Message } from '@/types';

// 导入多个类型
import type {
  GraphData,
  Node,
  NodeStatus,
  ApiResponse,
} from '@/types';
```

### 使用类型

```typescript
// 定义用户对象
const user: User = {
  id: '1',
  username: 'testuser',
  email: 'test@example.com',
  createdAt: new Date().toISOString(),
};

// 定义消息对象
const message: Message = {
  id: 'msg-1',
  sessionId: 'session-1',
  role: 'user',
  content: '你好',
  timestamp: new Date().toISOString(),
};

// 定义 API 响应
const response: ApiResponse<User> = {
  success: true,
  data: user,
};
```

## 类型验证

所有类型定义都经过了完整的测试验证，确保：

1. ✅ 类型定义完整且符合设计文档
2. ✅ 所有必需字段都已定义
3. ✅ 可选字段正确标记
4. ✅ 类型可以正常实例化和使用
5. ✅ TypeScript 编译检查通过

运行测试：

```bash
npm run test -- src/types/__test__/types.test.ts
```

## 设计原则

1. **类型安全**: 所有类型都是强类型，避免使用 `any`
2. **可扩展性**: 使用接口而非类型别名，便于扩展
3. **文档化**: 所有类型都有 JSDoc 注释说明
4. **一致性**: 命名和结构遵循统一的规范
5. **可维护性**: 类型按功能模块分类组织

## 注意事项

1. 所有日期时间字段使用 ISO 8601 格式字符串
2. ID 字段统一使用 string 类型
3. 可选字段使用 `?` 标记
4. 元数据字段使用 `Record<string, any>` 类型
5. 枚举类型使用字符串字面量联合类型

## 相关文档

- [设计文档](../../../.kiro/specs/frontend-ui-system/design.md)
- [需求文档](../../../.kiro/specs/frontend-ui-system/requirements.md)
- [任务列表](../../../.kiro/specs/frontend-ui-system/tasks.md)
