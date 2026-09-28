package repository

import (
	"context"
	"database/sql"
	"fmt"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/service"
)

// accountUpstreamCookieStore 持久化「发往 chatgpt.com 的出站 cookie jar」。
//
// 表结构见 migrations/241_openai_upstream_cookies.sql：按 (account_id, host, name) 一行，
// 值以 AES-256-GCM 密文存储（与其它账号密钥同一套 encryptor）。明文只在内存里出现，
// 绝不出现在日志、接口响应或错误信息中。
type accountUpstreamCookieStore struct {
	db        *sql.DB
	encryptor service.SecretEncryptor
	now       func() time.Time
}

// NewAccountUpstreamCookieStore 创建按账号隔离的上游 cookie 存储。
func NewAccountUpstreamCookieStore(db *sql.DB, encryptor service.SecretEncryptor) service.UpstreamCookieStore {
	return &accountUpstreamCookieStore{db: db, encryptor: encryptor, now: time.Now}
}

// LoadUpstreamCookies 读取未过期的 cookie。密文损坏、名字不在白名单或值不合法的行会被跳过，
// 不阻断主请求（缺失的 cookie 会由上游下一次 Set-Cookie 补齐）。
func (s *accountUpstreamCookieStore) LoadUpstreamCookies(ctx context.Context, accountID int64, host string) ([]service.UpstreamCookie, error) {
	if s == nil || s.db == nil || s.encryptor == nil || accountID <= 0 || host == "" {
		return nil, nil
	}
	rows, err := s.db.QueryContext(ctx, `
		SELECT name, value_cipher, expires_at
		FROM account_upstream_cookies
		WHERE account_id = $1 AND host = $2
		  AND (expires_at IS NULL OR expires_at > $3)
	`, accountID, host, s.now().UTC())
	if err != nil {
		return nil, fmt.Errorf("load upstream cookies: %w", err)
	}
	defer func() { _ = rows.Close() }()

	cookies := make([]service.UpstreamCookie, 0, 4)
	for rows.Next() {
		var (
			name       string
			ciphertext string
			expiresAt  sql.NullTime
		)
		if err := rows.Scan(&name, &ciphertext, &expiresAt); err != nil {
			return nil, fmt.Errorf("scan upstream cookie: %w", err)
		}
		if !service.IsAllowedOpenAIUpstreamCookieName(name) {
			continue
		}
		value, err := s.encryptor.Decrypt(ciphertext)
		if err != nil {
			continue
		}
		if !service.IsValidOpenAIUpstreamCookieValue(value) {
			continue
		}
		cookie := service.UpstreamCookie{Name: name, Value: value}
		if expiresAt.Valid {
			cookie.ExpiresAt = expiresAt.Time.UTC()
		}
		cookies = append(cookies, cookie)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate upstream cookies: %w", err)
	}
	return cookies, nil
}

// SaveUpstreamCookies 用给定集合整体替换该 (账号, 主机) 的 jar。
//
// 整体替换而不是逐条 upsert：调用方传入的始终是合并后的完整集合，替换语义同时覆盖
// “上游明确删除某个 cookie”的场景，且避免遗留过期行。
func (s *accountUpstreamCookieStore) SaveUpstreamCookies(ctx context.Context, accountID int64, host string, cookies []service.UpstreamCookie) error {
	if s == nil || s.db == nil || s.encryptor == nil || accountID <= 0 || host == "" {
		return nil
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin upstream cookie tx: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	if _, err := tx.ExecContext(ctx, `DELETE FROM account_upstream_cookies WHERE account_id = $1 AND host = $2`, accountID, host); err != nil {
		return fmt.Errorf("clear upstream cookies: %w", err)
	}
	now := s.now().UTC()
	for _, cookie := range cookies {
		if !service.IsAllowedOpenAIUpstreamCookieName(cookie.Name) {
			continue
		}
		if !service.IsValidOpenAIUpstreamCookieValue(cookie.Value) {
			continue
		}
		ciphertext, err := s.encryptor.Encrypt(cookie.Value)
		if err != nil {
			return fmt.Errorf("encrypt upstream cookie: %w", err)
		}
		var expiresAt any
		if !cookie.ExpiresAt.IsZero() {
			expiresAt = cookie.ExpiresAt.UTC()
		}
		if _, err := tx.ExecContext(ctx, `
			INSERT INTO account_upstream_cookies (account_id, host, name, value_cipher, expires_at, updated_at)
			VALUES ($1, $2, $3, $4, $5, $6)
		`, accountID, host, cookie.Name, ciphertext, expiresAt, now); err != nil {
			return fmt.Errorf("save upstream cookie: %w", err)
		}
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit upstream cookies: %w", err)
	}
	return nil
}

// ClearUpstreamCookies 丢弃该 (账号, 主机) 的全部 cookie（上游拒绝时的自愈动作）。
func (s *accountUpstreamCookieStore) ClearUpstreamCookies(ctx context.Context, accountID int64, host string) error {
	if s == nil || s.db == nil || accountID <= 0 || host == "" {
		return nil
	}
	if _, err := s.db.ExecContext(ctx, `DELETE FROM account_upstream_cookies WHERE account_id = $1 AND host = $2`, accountID, host); err != nil {
		return fmt.Errorf("clear upstream cookies: %w", err)
	}
	return nil
}
