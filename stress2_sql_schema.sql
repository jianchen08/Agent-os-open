-- ============================================================
-- 压力测试2 - SQL 数据库建表脚本
-- 包含：用户表、商品表、订单表
-- 特性：外键关系、索引、初始化示例数据
-- ============================================================

-- ------------------------------------------------------------
-- 1. 用户表 (users)
-- ------------------------------------------------------------
DROP TABLE IF EXISTS `orders`;
DROP TABLE IF EXISTS `products`;
DROP TABLE IF EXISTS `users`;

CREATE TABLE `users` (
    `id`         BIGINT       NOT NULL AUTO_INCREMENT COMMENT '用户ID',
    `username`   VARCHAR(64)  NOT NULL                COMMENT '用户名',
    `email`      VARCHAR(255) NOT NULL                COMMENT '邮箱',
    `password`   VARCHAR(255) NOT NULL                COMMENT '密码(加密)',
    `phone`      VARCHAR(20)  DEFAULT NULL            COMMENT '手机号',
    `status`     TINYINT      NOT NULL DEFAULT 1      COMMENT '状态: 1-正常 0-禁用',
    `created_at` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `updated_at` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_username` (`username`),
    UNIQUE KEY `uk_email` (`email`),
    KEY `idx_phone` (`phone`),
    KEY `idx_status` (`status`),
    KEY `idx_created_at` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户表';

-- ------------------------------------------------------------
-- 2. 商品表 (products)
-- ------------------------------------------------------------
CREATE TABLE `products` (
    `id`          BIGINT        NOT NULL AUTO_INCREMENT COMMENT '商品ID',
    `name`        VARCHAR(255)  NOT NULL                COMMENT '商品名称',
    `description` TEXT          DEFAULT NULL            COMMENT '商品描述',
    `category`    VARCHAR(64)   NOT NULL                COMMENT '分类',
    `price`       DECIMAL(10,2) NOT NULL                COMMENT '价格',
    `stock`       INT           NOT NULL DEFAULT 0      COMMENT '库存数量',
    `status`      TINYINT       NOT NULL DEFAULT 1      COMMENT '状态: 1-上架 0-下架',
    `created_at`  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `updated_at`  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (`id`),
    KEY `idx_category` (`category`),
    KEY `idx_price` (`price`),
    KEY `idx_status` (`status`),
    KEY `idx_name` (`name`),
    KEY `idx_created_at` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='商品表';

-- ------------------------------------------------------------
-- 3. 订单表 (orders)
-- ------------------------------------------------------------
CREATE TABLE `orders` (
    `id`          BIGINT        NOT NULL AUTO_INCREMENT COMMENT '订单ID',
    `user_id`     BIGINT        NOT NULL                COMMENT '用户ID',
    `product_id`  BIGINT        NOT NULL                COMMENT '商品ID',
    `quantity`    INT           NOT NULL DEFAULT 1      COMMENT '购买数量',
    `total_price` DECIMAL(12,2) NOT NULL                COMMENT '订单总金额',
    `status`      TINYINT       NOT NULL DEFAULT 0      COMMENT '状态: 0-待支付 1-已支付 2-已发货 3-已完成 4-已取消',
    `remark`      VARCHAR(512)  DEFAULT NULL            COMMENT '备注',
    `created_at`  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `updated_at`  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (`id`),
    KEY `idx_user_id` (`user_id`),
    KEY `idx_product_id` (`product_id`),
    KEY `idx_status` (`status`),
    KEY `idx_created_at` (`created_at`),
    KEY `idx_user_status` (`user_id`, `status`),
    CONSTRAINT `fk_orders_user`    FOREIGN KEY (`user_id`)    REFERENCES `users`    (`id`) ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT `fk_orders_product` FOREIGN KEY (`product_id`) REFERENCES `products` (`id`) ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='订单表';

-- ============================================================
-- 初始化示例数据
-- ============================================================

-- 用户数据 (10 条)
INSERT INTO `users` (`username`, `email`, `password`, `phone`, `status`) VALUES
('zhangsan',  'zhangsan@example.com',  'hashed_pwd_001', '13800000001', 1),
('lisi',      'lisi@example.com',      'hashed_pwd_002', '13800000002', 1),
('wangwu',    'wangwu@example.com',    'hashed_pwd_003', '13800000003', 1),
('zhaoliu',   'zhaoliu@example.com',   'hashed_pwd_004', '13800000004', 1),
('sunqi',     'sunqi@example.com',     'hashed_pwd_005', '13800000005', 1),
('zhouba',    'zhouba@example.com',    'hashed_pwd_006', '13800000006', 1),
('wujiu',     'wujiu@example.com',     'hashed_pwd_007', '13800000007', 1),
('zhengshi',  'zhengshi@example.com',  'hashed_pwd_008', '13800000008', 0),
('qianyi',    'qianyi@example.com',    'hashed_pwd_009', '13800000009', 1),
('chenmo',    'chenmo@example.com',    'hashed_pwd_010', '13800000010', 1);

-- 商品数据 (10 条)
INSERT INTO `products` (`name`, `description`, `category`, `price`, `stock`, `status`) VALUES
('iPhone 16 Pro',    '苹果最新旗舰手机',     '手机',   8999.00,  500, 1),
('MacBook Air M4',   '轻薄高性能笔记本',     '电脑',   9999.00,  200, 1),
('AirPods Pro 3',    '主动降噪无线耳机',     '耳机',   1999.00, 1000, 1),
('iPad Air',         '高性价比平板电脑',     '平板',   4799.00,  300, 1),
('Apple Watch Ultra','专业运动智能手表',     '手表',   5999.00,  150, 1),
('小米15 Ultra',     '安卓旗舰拍照手机',     '手机',   5499.00,  800, 1),
('华为 MatePad Pro', '鸿蒙系统平板电脑',     '平板',   3699.00,  400, 1),
('Sony WH-1000XM6', '降噪耳机天花板',       '耳机',   2499.00,  600, 1),
('ThinkPad X1 Carbon','商务旗舰轻薄本',      '电脑',  10999.00,  100, 1),
('机械革命旷世16',   '高性能游戏本',         '电脑',   6999.00,   50, 0);

-- 订单数据 (15 条)
INSERT INTO `orders` (`user_id`, `product_id`, `quantity`, `total_price`, `status`, `remark`) VALUES
(1, 1, 1,  8999.00, 1, '请尽快发货'),
(1, 3, 2,  3998.00, 3, NULL),
(2, 2, 1,  9999.00, 2, '需要开发票'),
(2, 5, 1,  5999.00, 0, NULL),
(3, 6, 1,  5499.00, 1, NULL),
(3, 7, 2,  7398.00, 3, '送人的礼物'),
(4, 4, 1,  4799.00, 2, NULL),
(4, 8, 1,  2499.00, 3, NULL),
(5, 1, 2, 17998.00, 1, '公司采购'),
(5, 9, 1, 10999.00, 0, NULL),
(6, 10, 1, 6999.00, 4, '商品已下架，取消订单'),
(7, 3, 3,  5997.00, 3, NULL),
(8, 2, 1,  9999.00, 1, '需要红色款'),
(9, 6, 1,  5499.00, 2, NULL),
(10, 4, 2,  9598.00, 1, NULL);
