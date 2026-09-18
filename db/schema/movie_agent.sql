-- =============================================================================
-- movie_agent 业务库表结构
-- -----------------------------------------------------------------------------
-- 与语料库 imdb 隔离；host / 账号 / 密码复用同一 MySQL 实例的 IMDB_MYSQL_*，仅库名不同。
-- 本文件内的 DDL 是「手动初始化」的唯一来源，**业务代码禁止包含任何建库建表语句**；
-- 业务代码只做「表是否存在 / 读写是否正常」探测，异常则强 MySQL fail-fast（服务拒绝启动，无 file 兜底）。
--
-- 初始化方式（二选一）：
--   A. 一键脚本：  .venv/Scripts/python.exe scripts/init_movie_agent_db.py
--   B. 手动执行：  在 mysql 客户端切到 movie_agent 库后 source 本文件（已含 CREATE DATABASE/USE）
-- =============================================================================

CREATE DATABASE IF NOT EXISTS `movie_agent` DEFAULT CHARACTER SET utf8mb4;
USE `movie_agent`;

-- G4 偏好记忆：单一 JSON 列存整份 {user_id, preferences, history}
-- Agent（remember_preference 工具）与前端 /memory 接口共用同一张表 = 真正打通
CREATE TABLE IF NOT EXISTS `user_preferences` (
  `user_id`    VARCHAR(64)  NOT NULL PRIMARY KEY,
  `data`       JSON        NOT NULL,
  `updated_at` DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- G3 会话：messages 以 JSON 列内嵌（与 file 兜底结构完全一致）
CREATE TABLE IF NOT EXISTS `sessions` (
  `session_id` VARCHAR(64)  NOT NULL PRIMARY KEY,
  `user_id`    VARCHAR(64)  NOT NULL,
  `title`      VARCHAR(255) NOT NULL DEFAULT '新会话',
  `created_at` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `messages`   JSON         NOT NULL,
  KEY `idx_sessions_user` (`user_id`, `updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- G-Share 会话共享：session_id -> 不可猜测令牌（一个会话至多一个有效令牌）。
-- 公开只读查看 GET /share/{token} 仅凭令牌读取，不暴露 owner；未共享/令牌无效返回 404。
CREATE TABLE IF NOT EXISTS `shared_sessions` (
  `session_id`      VARCHAR(64)  NOT NULL PRIMARY KEY,
  `owner_user_id`   VARCHAR(64)  NOT NULL,
  `share_token`     VARCHAR(64)  NOT NULL,
  `created_at`      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY `uniq_shared_token` (`share_token`),
  KEY `idx_shared_owner` (`owner_user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- G1 用户账户（G1 落地时启用；此处先备好 DDL，便于一键初始化）
CREATE TABLE IF NOT EXISTS `users` (
  `username`   VARCHAR(64)  NOT NULL PRIMARY KEY,
  `user_id`    VARCHAR(64)  NOT NULL,
  `salt`       VARCHAR(64)  NOT NULL,
  `pw_hash`    VARCHAR(256) NOT NULL,
  `created_at` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY `uniq_users_userid` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- O11 用户反馈闭环：行为反馈原始流水（强 MySQL，应用层保证幂等/去重）
-- 信号：explicit_like / explicit_dislike（前端赞踩） / click（卡片点击） / impression（推荐曝光）
-- genres 仅显式赞踩携带，用于聚合类型亲和度；impression 记 rank 用于位置偏差校正
CREATE TABLE IF NOT EXISTS `user_feedback` (
  `id`         BIGINT      NOT NULL AUTO_INCREMENT PRIMARY KEY,
  `user_id`    VARCHAR(64) NOT NULL,
  `item_id`    VARCHAR(64) NOT NULL,          -- 与推荐一致：douban:{id} / imdb:{id}
  `item_kind`  VARCHAR(16) NOT NULL DEFAULT 'movie',
  `signal`     VARCHAR(24) NOT NULL,
  `rank`       SMALLINT    NULL,              -- 当次推荐中的名次（曝光信号用）
  `query`      VARCHAR(255) NULL,             -- 触发推荐的 query（上下文）
  `genres`     VARCHAR(255) NULL,             -- 逗号分隔，仅显式赞踩携带
  `weight`     FLOAT       NOT NULL DEFAULT 1.0,
  `created_at` DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY `idx_fb_user` (`user_id`, `created_at`),
  KEY `idx_fb_item` (`user_id`, `item_id`, `signal`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
