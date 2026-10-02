package tlsfingerprint

import (
	"bytes"
	"net"
	"sort"
	"strconv"
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
	Host string // 只对该 host 生效（大小写不敏感）
	// Paths 只对这些路径**精确匹配**生效（不含子路径）；为空表示该 host 的全部路径。
	// 之所以不做前缀匹配：`/backend-api/codex/responses` 是 `/responses/compact` 的前缀，
	// 而 compact 是另一条协议线、我们没有它的官方顺序靶子，误命中会声明一个未实测的形态。
	Paths []string
	Order []string // 头名（小写），按期望顺序排列
}

// OpenAIResponsesHeaderOrder 是官方 codex 客户端 HTTP/1.1
// POST /backend-api/codex/responses 的实测头顺序。
//
// 采集（两轮，均在东京机做受控劫持：记录 req.rawHeaders 即原样线上顺序，
// 并对 WS 升级回可重试的 503 逼其回退到 HTTPS POST）：
//   - 2026-09-28：官方 codex-cli 0.157.0 与 0.158.0 各 2 条样本（model=gpt-5.1-codex，
//     该模型不是 Responses Lite 模型，报文里没有 lite 头），四次完全一致。
//   - 2026-09-29：换成我们实际用的模型串重采（model=gpt-6-astra，官方内置模型目录里
//     该模型的 use_responses_lite=true），0.157.0 与 0.158.0 各 2 条，四次完全一致 ——
//     报文里出现 x-openai-internal-codex-responses-lite，位置固定在
//     x-codex-turn-metadata 之后、x-codex-routing-hint 之前（第 5 位），
//     并非排在 host 之前。
//   - 2026-09-29（同日第三轮）：用"伪造一次成功开始"的办法逼客户端回放
//     x-codex-turn-state —— 劫持下把主请求回成 200 + 该响应头 + 一条 response.created，
//     再在 response.completed 之前掐断连接（官方把这种断流归为可重试的
//     CodexErrorDetails::Stream）。同一会话的 6 条主请求里，首条无该头、后续 5 条
//     全部带它（值即服务端下发的探针值），位置固定在 x-codex-beta-features 之后、
//     x-codex-window-id 之前（第 3 位）。
//
// 表外的头统一排在 host 之前、并保持它们原有的相对顺序——这是回退规则，
// 目前尚无实测样本落在其上（曾经落在这里的 lite 头、turn-state 头现已按实测位置进表）。
//
// 另外，官方 HTTP/1.1 请求的头名**全部小写**（实测 4/4 样本，含 host 与
// content-length；WebSocket 升级请求则是另一套写法，不适用本规则）。Go 写的是
// 规范形式（Host / Content-Length / Accept…），因此重排时一并改成小写。
//
// 作用范围只有这条精确路径：`/backend-api/codex/responses/compact`（compact 客户端
// 端点）与 `/responses/{id}/cancel` 之类的子路径**不**套用本表 —— compact 是另一条
// 协议线，我们没有它的官方顺序样本，宁可不改也不声明一个未实测的形态。
func OpenAIResponsesHeaderOrder() *HTTPHeaderOrder {
	return &HTTPHeaderOrder{
		Host:  "chatgpt.com",
		Paths: []string{"/backend-api/codex/responses"},
		Order: []string{
			"version",
			"x-codex-beta-features",
			"x-codex-turn-state", // 仅当本次回合的服务端响应下发过这张票时出现，位置固定在此（第 3 位）
			"x-codex-window-id",
			"x-codex-turn-metadata",
			"x-openai-internal-codex-responses-lite", // 仅当官方模型的 use_responses_lite=true 时出现，位置固定在此
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
	rules []*HTTPHeaderOrder
	buf   []byte
	// bodyLeft 是当前请求仍需原样透传的正文长度。头块写完之后只按长度跳过正文、
	// 不扫描其中的字节，避免把正文里出现的 CRLFCRLF 误判成下一个请求的头块。
	bodyLeft int64
	// passthrough 为真时之后的所有字节原样透传（无法安全确定正文边界、或头块异常大）。
	passthrough bool
}

// NewOrderedWriteConn 包装一个（已完成握手的）连接：命中规则的 HTTP/1.1 请求
// 会按声明的头顺序写出；同一条连接上后续的每个请求都会重新判定，
// 未命中规则的请求、正文、以及所有非请求字节一律原样透传。
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
	if c.passthrough {
		return c.Conn.Write(p)
	}
	total := len(p)

	// 正在透传正文：按剩余长度原样写出，剩下没写完的才是「下一个请求的开头」。
	if c.bodyLeft > 0 {
		n := int64(len(p))
		if n > c.bodyLeft {
			n = c.bodyLeft
		}
		if _, err := c.Conn.Write(p[:n]); err != nil {
			return 0, err
		}
		c.bodyLeft -= n
		p = p[n:]
		if len(p) == 0 {
			return total, nil
		}
	}

	c.buf = append(c.buf, p...)
	end := bytes.Index(c.buf, headerTerminator)
	if end < 0 {
		if len(c.buf) > maxOrderedHeaderBytes {
			// 头块异常大：原样透传，本连接之后不再重排。
			return c.flushAsIs(total)
		}
		// 头块还没写完，先记下；调用方认为本次写入已完成。
		return total, nil
	}
	head := c.buf[:end+len(headerTerminator)]
	rest := c.buf[end+len(headerTerminator):]
	c.buf = nil

	out := c.reorder(head)
	block := make([]byte, 0, len(out)+len(rest))
	block = append(block, out...)
	block = append(block, rest...)
	if _, err := c.Conn.Write(block); err != nil {
		return 0, err
	}

	// 头块紧邻的字节一定属于本请求的正文，据此记下还需要原样透传多少字节。
	bodyLen, ok := outboundRequestBodyLength(out)
	if !ok {
		// 无法判断正文边界（如 chunked）：本连接之后一律原样透传，绝不误改正文。
		c.passthrough = true
		return total, nil
	}
	if left := bodyLen - int64(len(rest)); left > 0 {
		c.bodyLeft = left
	}
	return total, nil
}

func (c *orderedWriteConn) flushAsIs(n int) (int, error) {
	c.passthrough = true
	buf := c.buf
	c.buf = nil
	if _, err := c.Conn.Write(buf); err != nil {
		return 0, err
	}
	return n, nil
}

// outboundRequestBodyLength 从（已重排的）头块里取出正文长度。
// 没有 content-length 也没有 transfer-encoding 表示本次请求没有正文（长度 0）；
// 出现 transfer-encoding（chunked 等无法定长的写法）或 content-length 非法时返回
// ok=false，调用方据此停止重排，避免把正文当成下一个请求。
func outboundRequestBodyLength(head []byte) (int64, bool) {
	body := head[:len(head)-len(headerTerminator)]
	for _, line := range bytes.Split(body, []byte("\r\n"))[1:] {
		colon := bytes.IndexByte(line, ':')
		if colon <= 0 {
			continue
		}
		name := strings.ToLower(strings.TrimSpace(string(line[:colon])))
		value := strings.TrimSpace(string(line[colon+1:]))
		switch name {
		case "content-length":
			n, err := strconv.ParseInt(value, 10, 64)
			if err != nil || n < 0 {
				return 0, false
			}
			return n, true
		case "transfer-encoding":
			return 0, false
		}
	}
	return 0, true
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
		for _, exact := range rule.Paths {
			if target == exact {
				return rule.Order
			}
		}
	}
	return nil
}
