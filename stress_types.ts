/** 用户角色枚举 */
export enum UserRole {
  ADMIN = "admin",
  USER = "user",
  GUEST = "guest",
}

/** 用户状态枚举 */
export enum UserStatus {
  ACTIVE = "active",
  INACTIVE = "inactive",
  SUSPENDED = "suspended",
}

/** 用户实体接口 */
export interface User {
  id: string;
  username: string;
  email: string;
  role: UserRole;
  status: UserStatus;
  createdAt: string;
  updatedAt: string;
}

/** 创建用户 DTO */
export interface UserCreateDTO {
  username: string;
  email: string;
  password: string;
  role?: UserRole;
}

/** 更新用户 DTO */
export interface UserUpdateDTO {
  username?: string;
  email?: string;
  password?: string;
  role?: UserRole;
  status?: UserStatus;
}

/** 通用 API 响应接口 */
export interface ApiResponse<T> {
  code: number;
  message: string;
  data: T;
  timestamp: string;
}

/** 分页查询参数 */
export interface PaginationParams {
  page: number;
  pageSize: number;
  sortBy?: string;
  sortOrder?: "asc" | "desc";
}

/** 分页响应数据 */
export interface PaginatedData<T> {
  items: T[];
  total: number;
  page: number;
  pageSize: number;
  totalPages: number;
}
