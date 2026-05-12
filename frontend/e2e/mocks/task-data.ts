/**
 * 任务执行闭环测试 Mock 数据
 *
 * 提供测试用的模拟数据
 *
 * @docs docs/tasks/task-execution-loop-system.md
 */

import type { Task, Project, AcceptanceCriterion } from '@/types/task';

/**
 * 模拟任务数据
 */
export const mockTasks = {
  /**
   * 简单任务
   */
  simpleTask: {
    id: 'task-simple-001',
    title: '实现用户登录功能',
    goal: '实现用户登录功能，支持用户名密码登录和JWT Token认证',
    description: '实现用户登录API，包括登录验证、Token生成和验证',
    status: 'pending' as const,
    taskType: 'execution' as const,
    currentPhase: 'prepare' as const,
    agentLevel: 2 as const,
    acceptanceCriteria: [
      {
        id: 'ac-001',
        taskId: 'task-simple-001',
        description: '支持用户名密码登录',
        evaluatorType: 'tool' as const,
        evaluatorId: 'test_runner',
        status: 'pending' as const,
      },
      {
        id: 'ac-002',
        taskId: 'task-simple-001',
        description: '支持JWT Token认证',
        evaluatorType: 'tool' as const,
        evaluatorId: 'test_runner',
        status: 'pending' as const,
      },
      {
        id: 'ac-003',
        taskId: 'task-simple-001',
        description: '通过安全测试',
        evaluatorType: 'tool' as const,
        evaluatorId: 'security_checker',
        status: 'pending' as const,
      },
    ],
    phaseStatus: {
      prepare: {
        status: 'pending' as const,
      },
      execute: {
        status: 'pending' as const,
      },
      evaluate: {
        status: 'pending' as const,
      },
    },
    timestamps: {
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    },
  } as Task,

  /**
   * 复杂任务
   */
  complexTask: {
    id: 'task-complex-001',
    title: '实现完整的用户认证系统',
    goal: '实现完整的用户认证系统，包括注册、登录、密码重置和邮箱验证',
    description: '构建一个安全可靠的用户认证系统',
    status: 'in_progress' as const,
    taskType: 'execution' as const,
    currentPhase: 'execute' as const,
    agentLevel: 2 as const,
    acceptanceCriteria: [
      {
        id: 'ac-complex-001',
        taskId: 'task-complex-001',
        description: '用户注册功能',
        evaluatorType: 'tool' as const,
        evaluatorId: 'test_runner',
        status: 'passed' as const,
        result: {
          passed: true,
          message: '所有测试通过',
        },
        evaluatedAt: new Date().toISOString(),
      },
      {
        id: 'ac-complex-002',
        taskId: 'task-complex-001',
        description: '用户登录功能',
        evaluatorType: 'tool' as const,
        evaluatorId: 'test_runner',
        status: 'passed' as const,
        result: {
          passed: true,
          message: '所有测试通过',
        },
        evaluatedAt: new Date().toISOString(),
      },
      {
        id: 'ac-complex-003',
        taskId: 'task-complex-001',
        description: '密码重置功能',
        evaluatorType: 'tool' as const,
        evaluatorId: 'test_runner',
        status: 'evaluating' as const,
      },
      {
        id: 'ac-complex-004',
        taskId: 'task-complex-001',
        description: '邮箱验证功能',
        evaluatorType: 'agent' as const,
        evaluatorId: 'email-verifier',
        status: 'pending' as const,
      },
    ],
    phaseStatus: {
      prepare: {
        status: 'completed' as const,
        startTime: new Date(Date.now() - 60000).toISOString(),
        endTime: new Date(Date.now() - 30000).toISOString(),
        output: {
          research: '已完成需求调研',
          plan: '已制定执行计划',
        },
        durationMs: 30000,
      },
      execute: {
        status: 'running' as const,
        startTime: new Date(Date.now() - 30000).toISOString(),
      },
      evaluate: {
        status: 'pending' as const,
      },
    },
    timestamps: {
      startedAt: new Date(Date.now() - 60000).toISOString(),
      createdAt: new Date(Date.now() - 60000).toISOString(),
      updatedAt: new Date().toISOString(),
    },
  } as Task,

  /**
   * 已完成任务
   */
  completedTask: {
    id: 'task-completed-001',
    title: '实现数据库连接池',
    goal: '实现高效的数据库连接池',
    status: 'completed' as const,
    taskType: 'execution' as const,
    currentPhase: 'evaluate' as const,
    agentLevel: 2 as const,
    acceptanceCriteria: [
      {
        id: 'ac-completed-001',
        taskId: 'task-completed-001',
        description: '连接池初始化',
        evaluatorType: 'tool' as const,
        evaluatorId: 'test_runner',
        status: 'passed' as const,
        result: {
          passed: true,
          message: '测试通过',
        },
        evaluatedAt: new Date().toISOString(),
      },
      {
        id: 'ac-completed-002',
        taskId: 'task-completed-001',
        description: '连接复用',
        evaluatorType: 'tool' as const,
        evaluatorId: 'test_runner',
        status: 'passed' as const,
        result: {
          passed: true,
          message: '测试通过',
        },
        evaluatedAt: new Date().toISOString(),
      },
    ],
    phaseStatus: {
      prepare: {
        status: 'completed' as const,
        startTime: new Date(Date.now() - 120000).toISOString(),
        endTime: new Date(Date.now() - 90000).toISOString(),
        durationMs: 30000,
      },
      execute: {
        status: 'completed' as const,
        startTime: new Date(Date.now() - 90000).toISOString(),
        endTime: new Date(Date.now() - 60000).toISOString(),
        durationMs: 30000,
      },
      evaluate: {
        status: 'completed' as const,
        startTime: new Date(Date.now() - 60000).toISOString(),
        endTime: new Date(Date.now() - 30000).toISOString(),
        output: {
          summary: '任务完成，所有验收标准通过',
          passedCount: 2,
          totalCount: 2,
        },
        durationMs: 30000,
      },
    },
    timestamps: {
      startedAt: new Date(Date.now() - 120000).toISOString(),
      completedAt: new Date(Date.now() - 30000).toISOString(),
      createdAt: new Date(Date.now() - 120000).toISOString(),
      updatedAt: new Date(Date.now() - 30000).toISOString(),
    },
  } as Task,

  /**
   * 失败任务
   */
  failedTask: {
    id: 'task-failed-001',
    title: '实现实时通信功能',
    goal: '实现WebSocket实时通信',
    status: 'failed' as const,
    taskType: 'execution' as const,
    currentPhase: 'execute' as const,
    agentLevel: 2 as const,
    acceptanceCriteria: [
      {
        id: 'ac-failed-001',
        taskId: 'task-failed-001',
        description: 'WebSocket连接建立',
        evaluatorType: 'tool' as const,
        evaluatorId: 'test_runner',
        status: 'failed' as const,
        result: {
          passed: false,
          message: '连接超时',
          details: {
            error: 'Connection timeout after 30s',
            retryCount: 3,
          },
        },
        evaluatedAt: new Date().toISOString(),
      },
    ],
    phaseStatus: {
      prepare: {
        status: 'completed' as const,
        startTime: new Date(Date.now() - 90000).toISOString(),
        endTime: new Date(Date.now() - 60000).toISOString(),
        durationMs: 30000,
      },
      execute: {
        status: 'failed' as const,
        startTime: new Date(Date.now() - 60000).toISOString(),
        endTime: new Date(Date.now() - 30000).toISOString(),
        error: 'WebSocket连接失败',
        durationMs: 30000,
      },
      evaluate: {
        status: 'pending' as const,
      },
    },
    retry: {
      count: 3,
      max: 3,
    },
    errorMessage: '任务失败：WebSocket连接无法建立',
    timestamps: {
      startedAt: new Date(Date.now() - 90000).toISOString(),
      createdAt: new Date(Date.now() - 90000).toISOString(),
      updatedAt: new Date(Date.now() - 30000).toISOString(),
    },
  } as Task,
};

/**
 * 模拟长期任务（项目）数据
 */
export const mockProjects = {
  /**
   * 运行中的项目
   */
  runningProject: {
    id: 'project-running-001',
    userId: 'test-user-001',
    sessionId: 'session-001',
    goal: '重构认证模块',
    status: 'running' as const,
    autoExecute: true,
    currentTaskIndex: 1,
    tasks: [
      {
        id: 'task-project-001',
        projectId: 'project-running-001',
        title: '规划重构方案',
        taskType: 'planning' as const,
        status: 'completed' as const,
        currentPhase: 'evaluate' as const,
        acceptanceCriteria: [],
        phaseStatus: {
          prepare: { status: 'completed' as const },
          execute: { status: 'completed' as const },
          evaluate: { status: 'completed' as const },
        },
        timestamps: {
          createdAt: new Date(Date.now() - 300000).toISOString(),
          updatedAt: new Date(Date.now() - 270000).toISOString(),
        },
      },
      {
        id: 'task-project-002',
        projectId: 'project-running-001',
        title: '实现JWT认证',
        taskType: 'execution' as const,
        status: 'in_progress' as const,
        currentPhase: 'execute' as const,
        acceptanceCriteria: [
          {
            id: 'ac-project-002-001',
            taskId: 'task-project-002',
            description: 'Token生成和验证',
            evaluatorType: 'tool' as const,
            evaluatorId: 'test_runner',
            status: 'passed' as const,
          },
          {
            id: 'ac-project-002-002',
            taskId: 'task-project-002',
            description: 'Token刷新机制',
            evaluatorType: 'tool' as const,
            evaluatorId: 'test_runner',
            status: 'evaluating' as const,
          },
        ],
        phaseStatus: {
          prepare: { status: 'completed' as const },
          execute: { status: 'running' as const },
          evaluate: { status: 'pending' as const },
        },
        timestamps: {
          startedAt: new Date(Date.now() - 270000).toISOString(),
          createdAt: new Date(Date.now() - 270000).toISOString(),
          updatedAt: new Date().toISOString(),
        },
      },
      {
        id: 'task-project-003',
        projectId: 'project-running-001',
        title: '实现密码加密',
        taskType: 'execution' as const,
        status: 'pending' as const,
        currentPhase: 'prepare' as const,
        acceptanceCriteria: [
          {
            id: 'ac-project-003-001',
            taskId: 'task-project-003',
            description: '使用bcrypt加密',
            evaluatorType: 'tool' as const,
            evaluatorId: 'test_runner',
            status: 'pending' as const,
          },
        ],
        phaseStatus: {
          prepare: { status: 'pending' as const },
          execute: { status: 'pending' as const },
          evaluate: { status: 'pending' as const },
        },
        timestamps: {
          createdAt: new Date().toISOString(),
          updatedAt: new Date().toISOString(),
        },
      },
      {
        id: 'task-project-004',
        projectId: 'project-running-001',
        title: '总体评估',
        taskType: 'final_evaluation' as const,
        status: 'pending' as const,
        currentPhase: 'prepare' as const,
        acceptanceCriteria: [],
        phaseStatus: {
          prepare: { status: 'pending' as const },
          execute: { status: 'pending' as const },
          evaluate: { status: 'pending' as const },
        },
        timestamps: {
          createdAt: new Date().toISOString(),
          updatedAt: new Date().toISOString(),
        },
      },
    ],
    timestamps: {
      createdAt: new Date(Date.now() - 300000).toISOString(),
      updatedAt: new Date().toISOString(),
    },
  } as Project,

  /**
   * 暂停的项目
   */
  pausedProject: {
    id: 'project-paused-001',
    userId: 'test-user-001',
    sessionId: 'session-002',
    goal: '优化数据库性能',
    status: 'paused' as const,
    autoExecute: false,
    currentTaskIndex: 0,
    tasks: [],
    timestamps: {
      createdAt: new Date(Date.now() - 600000).toISOString(),
      updatedAt: new Date(Date.now() - 300000).toISOString(),
    },
  } as Project,

  /**
   * 已完成的项目
   */
  completedProject: {
    id: 'project-completed-001',
    userId: 'test-user-001',
    sessionId: 'session-003',
    goal: '实现用户权限系统',
    status: 'completed' as const,
    autoExecute: false,
    currentTaskIndex: 3,
    tasks: [
      {
        id: 'task-completed-project-001',
        projectId: 'project-completed-001',
        title: '规划权限系统',
        taskType: 'planning' as const,
        status: 'completed' as const,
        currentPhase: 'evaluate' as const,
        acceptanceCriteria: [],
        phaseStatus: {
          prepare: { status: 'completed' as const },
          execute: { status: 'completed' as const },
          evaluate: { status: 'completed' as const },
        },
        timestamps: {
          createdAt: new Date(Date.now() - 3600000).toISOString(),
          updatedAt: new Date(Date.now() - 3540000).toISOString(),
        },
      },
      {
        id: 'task-completed-project-002',
        projectId: 'project-completed-001',
        title: '实现RBAC权限控制',
        taskType: 'execution' as const,
        status: 'completed' as const,
        currentPhase: 'evaluate' as const,
        acceptanceCriteria: [
          {
            id: 'ac-completed-project-002-001',
            taskId: 'task-completed-project-002',
            description: '角色管理',
            evaluatorType: 'tool' as const,
            evaluatorId: 'test_runner',
            status: 'passed' as const,
          },
          {
            id: 'ac-completed-project-002-002',
            taskId: 'task-completed-project-002',
            description: '权限验证',
            evaluatorType: 'tool' as const,
            evaluatorId: 'test_runner',
            status: 'passed' as const,
          },
        ],
        phaseStatus: {
          prepare: { status: 'completed' as const },
          execute: { status: 'completed' as const },
          evaluate: { status: 'completed' as const },
        },
        timestamps: {
          createdAt: new Date(Date.now() - 3540000).toISOString(),
          updatedAt: new Date(Date.now() - 300000).toISOString(),
        },
      },
      {
        id: 'task-completed-project-003',
        projectId: 'project-completed-001',
        title: '总体评估',
        taskType: 'final_evaluation' as const,
        status: 'completed' as const,
        currentPhase: 'evaluate' as const,
        acceptanceCriteria: [
          {
            id: 'ac-completed-project-003-001',
            taskId: 'task-completed-project-003',
            description: '所有功能正常',
            evaluatorType: 'agent' as const,
            evaluatorId: 'evaluator',
            status: 'passed' as const,
          },
        ],
        phaseStatus: {
          prepare: { status: 'completed' as const },
          execute: { status: 'completed' as const },
          evaluate: { status: 'completed' as const },
        },
        timestamps: {
          createdAt: new Date(Date.now() - 300000).toISOString(),
          completedAt: new Date(Date.now() - 60000).toISOString(),
          updatedAt: new Date(Date.now() - 60000).toISOString(),
        },
      },
    ],
    timestamps: {
      createdAt: new Date(Date.now() - 3600000).toISOString(),
      updatedAt: new Date(Date.now() - 60000).toISOString(),
    },
  } as Project,
};

/**
 * 模拟WebSocket事件数据
 */
export const mockWebSocketEvents = {
  /**
   * 项目创建事件
   */
  projectCreated: {
    eventType: 'project_created' as const,
    projectId: 'project-test-001',
    goal: '测试项目创建',
    sessionId: 'session-test-001',
    createdAt: new Date().toISOString(),
  },

  /**
   * 项目进度更新事件
   */
  projectProgress: {
    eventType: 'project_progress' as const,
    projectId: 'project-test-001',
    currentTaskIndex: 1,
    totalTasks: 4,
    percentage: 25,
  },

  /**
   * 项目暂停事件
   */
  projectPaused: {
    eventType: 'project_paused' as const,
    projectId: 'project-test-001',
    pausedAt: new Date().toISOString(),
  },

  /**
   * 项目恢复事件
   */
  projectResumed: {
    eventType: 'project_resumed' as const,
    projectId: 'project-test-001',
    resumedAt: new Date().toISOString(),
  },

  /**
   * 任务创建事件
   */
  taskCreated: {
    eventType: 'task_created' as const,
    taskId: 'task-test-001',
    projectId: 'project-test-001',
    goal: '实现测试功能',
    taskType: 'execution' as const,
    phase: 'prepare' as const,
    createdAt: new Date().toISOString(),
  },

  /**
   * 任务阶段变更事件
   */
  taskPhaseChanged: {
    eventType: 'task_phase_changed' as const,
    taskId: 'task-test-001',
    phase: 'execute' as const,
    status: 'running' as const,
    previousPhase: 'prepare' as const,
    timestamp: new Date().toISOString(),
  },

  /**
   * 验收标准评估完成事件
   */
  taskACEvaluated: {
    eventType: 'task_ac_evaluated' as const,
    taskId: 'task-test-001',
    acId: 'ac-test-001',
    passed: true,
    result: {
      message: '验收标准通过',
      details: {
        score: 100,
        notes: '所有测试用例通过',
      },
    },
    evaluatedAt: new Date().toISOString(),
  },

  /**
   * 任务完成事件
   */
  taskCompleted: {
    eventType: 'task_completed' as const,
    taskId: 'task-test-001',
    projectId: 'project-test-001',
    result: {
      summary: '任务成功完成',
      output: {
        acceptanceCriteriaPassed: 3,
        acceptanceCriteriaTotal: 3,
        duration: 120000,
      },
    },
    completedAt: new Date().toISOString(),
  },

  /**
   * 任务失败事件
   */
  taskFailed: {
    eventType: 'task_failed' as const,
    taskId: 'task-test-001',
    projectId: 'project-test-001',
    error: '任务执行失败：连接超时',
    retryCount: 2,
    maxRetries: 3,
    canRetry: true,
    failedAt: new Date().toISOString(),
  },

  /**
   * 自动执行触发事件
   */
  autoExecuteTriggered: {
    eventType: 'auto_execute_triggered' as const,
    projectId: 'project-test-001',
    taskId: 'task-test-002',
    triggeredAt: new Date().toISOString(),
  },
};

/**
 * 模拟Agent Tab数据
 */
export const mockAgentTabs = {
  mainTab: {
    id: 'tab-main-001',
    agentId: 'agent-main-001',
    agentName: '主Agent',
    agentLevel: 1 as const,
    status: 'running' as const,
    hasUnread: false,
    canClose: false,
    path: ['主Agent'],
  },

  subTab1: {
    id: 'tab-sub-001',
    agentId: 'agent-sub-001',
    agentName: '规划Agent',
    agentLevel: 2 as const,
    taskId: 'task-test-001',
    status: 'completed' as const,
    hasUnread: false,
    canClose: true,
    path: ['主Agent', '规划Agent'],
  },

  subTab2: {
    id: 'tab-sub-002',
    agentId: 'agent-sub-002',
    agentName: '执行Agent',
    agentLevel: 2 as const,
    taskId: 'task-test-002',
    status: 'running' as const,
    hasUnread: true,
    canClose: true,
    path: ['主Agent', '执行Agent'],
  },

  subTab3: {
    id: 'tab-sub-003',
    agentId: 'agent-sub-003',
    agentName: '代码Agent',
    agentLevel: 3 as const,
    taskId: 'task-test-003',
    status: 'waiting_input' as const,
    hasUnread: true,
    canClose: true,
    path: ['主Agent', '执行Agent', '代码Agent'],
  },
};

/**
 * 获取随机任务ID
 */
export function getRandomTaskId(): string {
  return `task-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
}

/**
 * 获取随机项目ID
 */
export function getRandomProjectId(): string {
  return `project-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
}

/**
 * 获取随机Agent ID
 */
export function getRandomAgentId(): string {
  return `agent-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
}

/**
 * 创建Mock任务的工厂函数
 */
export function createMockTask(overrides?: Partial<Task>): Task {
  return {
    ...mockTasks.simpleTask,
    id: getRandomTaskId(),
    ...overrides,
  };
}

/**
 * 创建Mock项目的工厂函数
 */
export function createMockProject(overrides?: Partial<Project>): Project {
  return {
    ...mockProjects.runningProject,
    id: getRandomProjectId(),
    ...overrides,
  };
}
