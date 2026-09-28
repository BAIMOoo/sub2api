-- 241_openai_upstream_cookies.sql
--
-- 按账号保存发往 chatgpt.com 的出站 cookie（对齐官方 codex 客户端的 Cloudflare
-- 基础设施 cookie 白名单，见 internal/service/openai_upstream_cookie_jar.go）。
--
-- 值以应用层 AES-256-GCM 密文存储，明文只在内存里出现；本表不参与任何对外响应。
-- 上游拒绝（CF 挑战 / 403）时应用会清空对应行以自愈。

CREATE TABLE IF NOT EXISTS account_upstream_cookies (
    account_id   BIGINT      NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    host         TEXT        NOT NULL,
    name         TEXT        NOT NULL,
    value_cipher TEXT        NOT NULL,
    expires_at   TIMESTAMPTZ,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (account_id, host, name)
);

CREATE INDEX IF NOT EXISTS idx_account_upstream_cookies_account
    ON account_upstream_cookies (account_id);
