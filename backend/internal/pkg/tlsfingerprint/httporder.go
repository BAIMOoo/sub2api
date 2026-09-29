package tlsfingerprint

import (
	"bytes"
	"net"
	"sort"
	"strings"
)

// HTTPHeaderOrder 声明「某个 host（可按路径前缀收窄）上，请求头的期望线上顺序」。
//
// 之所以需要它：Go 的 net/http 写头顺序是固定的（请求行 → Host → User-Agent →
// Content-Length → 其余按 key 字节序），且不可配置；而官方客户端的顺序是它自己
// 的插入顺序。Go 源码里没有钩子，所以这里换一个不动 Transport 的做法：
// 在连接层把已经写出的「请求头块」缓存下来、重排后再发出去。
// 连接复用、SSE 流式读取、超时与取消仍然全部由 net/http 负责。
type HTTPHeaderOrder struct {
	Host  string   // 只对该 host 生效（大小写不敏感）
	Paths []string // 只对这些路径前缀生效；为空表示该 host 的全部路径
	Order []string // 头名（小写），按期望顺序排列
}

// OpenAIResponsesHeaderOrder 是官方 codex 客户端 HTTP/1.1
// POST /backend-api/codex/responses 的实测头顺序。
//
// 采集：2026-09-28 在东京机做受控劫持（记录 req.rawHeaders，即原样线上顺序，
// 并对 WS 升级回可重试的 503 逼其回退到 HTTPS POST），官方 codex-cli 0.157.0 与
// 0.158.0 各 2 条样本，四次顺序完全一致。
//
// 表外的头（例如本网关特有的 x-openai-internal-codex-responses-lite）统一排在
// host 之前、并保持它们原有的相对顺序——这是推断，不是实测。
//
// 另外，官方 HTTP/1.1 请求的头名**全部小写**（实测 4/4 样本，含 host 与
// content-length；WebSocket 升级请求则是另一套写法，不适用本规则）。Go 写的是
// 规范形式（Host / Content-Length / Accept…），因此重排时一并改成小写。
func OpenAIResponsesHeaderOrder() *HTTPHeaderOrder {
	return &HTTPHeaderOrder{
		Host:  "chatgpt.com",
		Paths: []string{"/backend-api/codex/responses"},
		Order: []string{
			"version",
			"x-codex-beta-features",
			"x-codex-window-id",
			"x-codex-turn-metadata",
			"x-codex-routing-hint",
			"x-client-request-id",
			"session-id",
			"thread-id",
			"accept",
			"content-encoding",
			"content-type",
			"authorization",
			"chatgpt-account-id",
			"originator",
			"user-agent",
			"cookie", // 仅当客户端 cookie jar 里有 cookie 时出现，位置固定在此
			"host",
			"content-length",
		},
	}
}

var headerTerminator = []byte("\r\n\r\n")

// HeaderOrders 是 HTTPHeaderOrders 的 nil 安全访问器（profile 可能为 nil）。
func (p *Profile) HeaderOrders() []*HTTPHeaderOrder {
	if p == nil {
		return nil
	}
	return p.HTTPHeaderOrders
}

// maxOrderedHeaderBytes 是等待完整请求头块的上限。超过就原样透传，
// 避免异常（超大或无终止符）的请求把内存拖大、或把数据卡在缓冲里。
const maxOrderedHeaderBytes = 64 << 10

type orderedWriteConn struct {
	net.Conn
	rules  []*HTTPHeaderOrder
	buf    []byte
	bypass bool
}

// NewOrderedWriteConn 包装一个（已完成握手的）连接：命中规则的 HTTP/1.1 请求
// 会按声明的头顺序写出，其余请求与所有后续字节一律原样透传。
// 没有任何可用规则时直接返回原连接。
func NewOrderedWriteConn(c net.Conn, rules ...*HTTPHeaderOrder) net.Conn {
	kept := make([]*HTTPHeaderOrder, 0, len(rules))
	for _, r := range rules {
		if r != nil && len(r.Order) > 0 {
			kept = append(kept, r)
		}
	}
	if len(kept) == 0 {
		return c
	}
	return &orderedWriteConn{Conn: c, rules: kept}
}

func (c *orderedWriteConn) Write(p []byte) (int, error) {
	if c.bypass {
		return c.Conn.Write(p)
	}
	c.buf = append(c.buf, p...)
	end := bytes.Index(c.buf, headerTerminator)
	if end < 0 {
		if len(c.buf) > maxOrderedHeaderBytes {
			// 头块异常大：原样透传，不要再缓冲。
			return c.flushAsIs(len(p))
		}
		// 头块还没写完，先记下；调用方认为本次写入已完成。
		return len(p), nil
	}
	head := c.buf[:end+len(headerTerminator)]
	rest := c.buf[end+len(headerTerminator):]
	c.buf = nil
	c.bypass = true
	out := c.reorder(head)
	out = append(out, rest...)
	if _, err := c.Conn.Write(out); err != nil {
		return 0, err
	}
	return len(p), nil
}

func (c *orderedWriteConn) flushAsIs(n int) (int, error) {
	c.bypass = true
	buf := c.buf
	c.buf = nil
	if _, err := c.Conn.Write(buf); err != nil {
		return 0, err
	}
	return n, nil
}

// reorder 只重排「请求行之后的头行」：请求行、头行的字节内容与整体长度都不变，
// 因此 Content-Length 语义不受影响。
func (c *orderedWriteConn) reorder(head []byte) []byte {
	body := head[:len(head)-len(headerTerminator)]
	lines := bytes.Split(body, []byte("\r\n"))
	if len(lines) < 2 {
		return head
	}
	order := c.match(lines)
	if order == nil {
		return head
	}

	// 排名放大 2 倍，留出一个空位给「表外的头」：它们排在 host 之前、
	// 并靠稳定排序保持原有相对顺序。
	rank := make(map[string]int, len(order))
	for i, name := range order {
		rank[name] = i * 2
	}
	unknown, ok := rank["host"]
	if !ok {
		unknown = len(order) * 2
	}
	unknown--

	type headerLine struct {
		name  []byte
		value []byte
		rank  int
	}
	fields := make([]headerLine, 0, len(lines)-1)
	for _, line := range lines[1:] {
		colon := bytes.IndexByte(line, ':')
		if colon <= 0 {
			continue
		}
		name := strings.ToLower(strings.TrimSpace(string(line[:colon])))
		r, known := rank[name]
		if !known {
			r = unknown
		}
		fields = append(fields, headerLine{name: []byte(name), value: line[colon+1:], rank: r})
	}
	sort.SliceStable(fields, func(a, b int) bool { return fields[a].rank < fields[b].rank })

	out := make([]byte, 0, len(head))
	out = append(out, lines[0]...)
	out = append(out, '\r', '\n')
	for _, f := range fields {
		out = append(out, f.name...)
		out = append(out, ':')
		out = append(out, f.value...)
		out = append(out, '\r', '\n')
	}
	out = append(out, '\r', '\n')
	return out
}

// match 从请求行与 Host 头里取出 (路径, host)，返回命中的顺序表；没有命中返回 nil。
func (c *orderedWriteConn) match(lines [][]byte) []string {
	parts := bytes.SplitN(lines[0], []byte(" "), 3)
	if len(parts) < 3 {
		return nil
	}
	target := string(parts[1])

	var host string
	for _, line := range lines[1:] {
		colon := bytes.IndexByte(line, ':')
		if colon <= 0 {
			continue
		}
		if strings.EqualFold(strings.TrimSpace(string(line[:colon])), "host") {
			host = strings.TrimSpace(string(line[colon+1:]))
			break
		}
	}

	for _, rule := range c.rules {
		if !strings.EqualFold(host, rule.Host) {
			continue
		}
		if len(rule.Paths) == 0 {
			return rule.Order
		}
		for _, prefix := range rule.Paths {
			if strings.HasPrefix(target, prefix) {
				return rule.Order
			}
		}
	}
	return nil
}
