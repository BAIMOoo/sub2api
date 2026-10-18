// Package codexprobe authenticates synthetic alignment probes and records only
// sanitized evidence. No header alone can turn an ordinary request into a probe.
package codexprobe

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"
)

const Header = "X-Sub2API-Alignment"
const maxBody = 2 << 20

var ErrProbe = errors.New("alignment probe environment or evidence unavailable")
var safeID = regexp.MustCompile("^[a-zA-Z0-9_-]{1,80}$")
var uuidID = regexp.MustCompile("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
var hexID = regexp.MustCompile("^[0-9a-f]{32}$")

func identityFormat(s string) string {
	if uuidID.MatchString(s) {
		return "uuid"
	}
	if hexID.MatchString(s) {
		return "hex32"
	}
	return fmt.Sprintf("other:%d", len(s))
}

type Config struct {
	Secret      string `json:"secret"`
	APIKeyID    int64  `json:"api_key_id"`
	AccountID   int64  `json:"account_id"`
	ProxyID     int64  `json:"proxy_id"`
	ProxySHA256 string `json:"proxy_sha256"`
	OutputDir   string `json:"output_dir"`
	MaxRequests int    `json:"max_requests"`
}

type Ticket struct {
	Run     string `json:"run"`
	Sample  string `json:"sample"`
	Expires int64  `json:"expires"`
}

type contextKey struct{}
type runState struct {
	mu       sync.Mutex
	count    int
	sequence int
	expires  int64
}
type Probe struct {
	cfg    Config
	ticket Ticket
	run    *runState
}

var registry = struct {
	sync.Mutex
	runs map[string]*runState
}{runs: make(map[string]*runState)}

func FromContext(ctx context.Context) *Probe {
	p, _ := ctx.Value(contextKey{}).(*Probe)
	return p
}

// Authenticate is called only after ordinary gateway API-key authentication.
// Configuration is deliberately read only for explicitly signed probes.
func Authenticate(req *http.Request, keyID int64) (*http.Request, error) {
	value := req.Header.Get(Header)
	if value == "" {
		return req, nil
	}
	path := os.Getenv("CODEX_ALIGNMENT_OBSERVER_CONFIG")
	if path == "" {
		return nil, ErrProbe
	}
	// Only the operator's process environment selects this configuration file;
	// request headers cannot supply or modify its path.
	raw, err := os.ReadFile(path) // #nosec G703 -- trusted deployment configuration path
	if err != nil {
		return nil, ErrProbe
	}
	var cfg Config
	if json.Unmarshal(raw, &cfg) != nil {
		return nil, ErrProbe
	}
	return authenticate(req, keyID, cfg, time.Now())
}

func authenticate(req *http.Request, keyID int64, cfg Config, now time.Time) (*http.Request, error) {
	if len(cfg.Secret) < 32 || cfg.APIKeyID <= 0 || keyID != cfg.APIKeyID ||
		cfg.AccountID <= 0 || cfg.ProxyID <= 0 || len(cfg.ProxySHA256) != 64 ||
		!filepath.IsAbs(cfg.OutputDir) || cfg.MaxRequests < 1 || cfg.MaxRequests > 256 {
		return nil, ErrProbe
	}
	value := req.Header.Get(Header)
	if len(value) > 1024 {
		return nil, ErrProbe
	}
	parts := strings.Split(value, ".")
	if len(parts) != 2 {
		return nil, ErrProbe
	}
	sig, err := hex.DecodeString(parts[1])
	if err != nil {
		return nil, ErrProbe
	}
	mac := hmac.New(sha256.New, []byte(cfg.Secret))
	_, _ = mac.Write([]byte(parts[0]))
	if !hmac.Equal(sig, mac.Sum(nil)) {
		return nil, ErrProbe
	}
	data, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		return nil, ErrProbe
	}
	var ticket Ticket
	if json.Unmarshal(data, &ticket) != nil || !safeID.MatchString(ticket.Run) ||
		!safeID.MatchString(ticket.Sample) || ticket.Expires <= now.Unix() ||
		ticket.Expires > now.Add(65*time.Minute).Unix() {
		return nil, ErrProbe
	}
	registry.Lock()
	for id, run := range registry.runs {
		if run.expires <= now.Unix() {
			delete(registry.runs, id)
		}
	}
	run := registry.runs[ticket.Run]
	if run == nil {
		if len(registry.runs) >= 256 {
			registry.Unlock()
			return nil, ErrProbe
		}
		run = &runState{expires: ticket.Expires}
		registry.runs[ticket.Run] = run
	} else if run.expires != ticket.Expires {
		registry.Unlock()
		return nil, ErrProbe
	}
	registry.Unlock()
	p := &Probe{cfg: cfg, ticket: ticket, run: run}
	copy := req.Clone(context.WithValue(req.Context(), contextKey{}, p))
	copy.Header.Del(Header)
	return copy, nil
}

func CheckSelection(ctx context.Context, accountID, proxyID int64, oauth bool) error {
	p := FromContext(ctx)
	if p == nil {
		return nil
	}
	if !oauth || accountID != p.cfg.AccountID || proxyID != p.cfg.ProxyID ||
		time.Now().Unix() >= p.ticket.Expires {
		return ErrProbe
	}
	return nil
}

func (p *Probe) CheckTransport(req *http.Request, accountID int64, proxyURL string) error {
	digest := sha256.Sum256([]byte(proxyURL))
	if accountID != p.cfg.AccountID || proxyURL == "" ||
		!hmac.Equal([]byte(hex.EncodeToString(digest[:])), []byte(p.cfg.ProxySHA256)) ||
		req.URL.Scheme != "https" || req.URL.Host != "chatgpt.com" ||
		!allowedPath(req.Method, req.URL.Path) || time.Now().Unix() >= p.ticket.Expires {
		return ErrProbe
	}
	return nil
}

func allowedPath(method, path string) bool {
	return method == "POST" && (path == "/backend-api/codex/responses" ||
		path == "/backend-api/codex/responses/compact") ||
		method == "GET" && path == "/backend-api/plugins/featured"
}

func (p *Probe) Deadline() time.Time { return time.Unix(p.ticket.Expires, 0) }

// reserve is called at the final HTTP write boundary, not once per CLI or
// service invocation. A failed write still consumes an attempt.
func (p *Probe) reserve(method, path string) error {
	p.run.mu.Lock()
	defer p.run.mu.Unlock()
	if !allowedPath(method, path) || time.Now().Unix() >= p.ticket.Expires ||
		p.run.count >= p.cfg.MaxRequests {
		return ErrProbe
	}
	dir := filepath.Join(p.cfg.OutputDir, p.ticket.Run)
	if os.MkdirAll(dir, 0750) != nil {
		return ErrProbe
	}
	// O_EXCL also preserves the hard attempt limit across application restarts.
	for i := 1; i <= p.cfg.MaxRequests; i++ {
		f, err := os.OpenFile(filepath.Join(dir, fmt.Sprintf("attempt-%03d", i)), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
		if os.IsExist(err) {
			continue
		}
		if err != nil {
			return ErrProbe
		}
		_ = f.Close()
		p.run.count = i
		return nil
	}
	return ErrProbe
}

func (p *Probe) Emit(kind string, data any) error {
	p.run.mu.Lock()
	p.run.sequence++
	seq := p.run.sequence
	count := p.run.count
	p.run.mu.Unlock()
	dir := filepath.Join(p.cfg.OutputDir, p.ticket.Run)
	if err := os.MkdirAll(dir, 0750); err != nil {
		return ErrProbe
	}
	record := map[string]any{
		"schema": 1, "run": p.ticket.Run, "sample": p.ticket.Sample,
		"kind": kind, "at": time.Now().UTC().Format(time.RFC3339Nano),
		"account_id": p.cfg.AccountID, "proxy_id": p.cfg.ProxyID,
		"attempts": count, "data": data,
	}
	raw, err := json.Marshal(record)
	if err != nil {
		return ErrProbe
	}
	// Atomic publication: readers never treat a partial JSON file as evidence.
	f, err := os.CreateTemp(dir, ".pending-")
	if err != nil {
		return ErrProbe
	}
	tmp := f.Name()
	defer func() { _ = os.Remove(tmp) }()
	// A setgid evidence root can grant a separate monitor group read access.
	// Only sanitized records are shared; tickets/config remain private.
	if err := f.Chmod(0640); err != nil {
		_ = f.Close()
		return ErrProbe
	}
	if _, err = f.Write(raw); err == nil {
		err = f.Close()
	} else {
		_ = f.Close()
	}
	if err != nil {
		return ErrProbe
	}
	name := fmt.Sprintf("%s-%06d-%s-%s.json", p.ticket.Sample, seq, kind, filepath.Base(tmp)[9:])
	if os.Rename(tmp, filepath.Join(dir, name)) != nil {
		return ErrProbe
	}
	return nil
}

func (p *Probe) opaque(s string) map[string]any {
	mac := hmac.New(sha256.New, []byte(p.cfg.Secret))
	_, _ = mac.Write([]byte(s))
	return map[string]any{"$hmac": hex.EncodeToString(mac.Sum(nil)), "bytes": len(s)}
}

// Text, prompts, tools, identifiers, and unknown string values are hashed.
// Comparisons retain every key and type without persisting arbitrary content.
func (p *Probe) sanitize(v any) any {
	switch x := v.(type) {
	case string:
		return p.opaque(x)
	case []any:
		out := make([]any, len(x))
		for i, item := range x {
			out[i] = p.sanitize(item)
		}
		return out
	case map[string]any:
		out := make(map[string]any, len(x))
		for key, item := range x {
			lower := strings.ToLower(key)
			if strings.Contains(lower, "token") && lower != "max_output_tokens" &&
				lower != "max_completion_tokens" && lower != "input_tokens" &&
				lower != "output_tokens" {
				out[key] = "<redacted>"
			} else if lower == "authorization" || lower == "cookie" || lower == "password" {
				out[key] = "<redacted>"
			} else {
				out[key] = p.sanitize(item)
			}
		}
		return out
	default:
		return v
	}
}
