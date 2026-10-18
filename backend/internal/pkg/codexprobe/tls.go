package codexprobe

import (
	"context"
	"encoding/binary"
	"net"
)

// WrapTLS observes the first ClientHello on the original tunnel below uTLS.
// It never persists randoms, session tickets, key shares, or encrypted records.
func WrapTLS(ctx context.Context, conn net.Conn) net.Conn {
	p := FromContext(ctx)
	if p == nil {
		return conn
	}
	return &tlsCapture{Conn: conn, probe: p}
}

type tlsCapture struct {
	net.Conn
	probe     *Probe
	records   []byte
	handshake []byte
	done      bool
}

func (c *tlsCapture) Write(b []byte) (int, error) {
	n, err := c.Conn.Write(b)
	if err != nil || n != len(b) || c.done {
		return n, err
	}
	c.records = append(c.records, b...)
	if len(c.records)+len(c.handshake) > 128<<10 {
		c.done = true
		_ = c.probe.Emit("incomplete", map[string]any{"reason": "client_hello_too_large"})
		return n, err
	}
	for len(c.records) >= 5 {
		size := int(binary.BigEndian.Uint16(c.records[3:5]))
		if len(c.records) < 5+size {
			break
		}
		if c.records[0] != 22 {
			c.done = true
			break
		}
		c.handshake = append(c.handshake, c.records[5:5+size]...)
		c.records = c.records[5+size:]
		if len(c.handshake) < 4 {
			continue
		}
		length := int(c.handshake[1])<<16 | int(c.handshake[2])<<8 | int(c.handshake[3])
		if len(c.handshake) < length+4 {
			continue
		}
		c.done = true
		hello, ok := clientHello(c.handshake[:length+4])
		if !ok {
			_ = c.probe.Emit("incomplete", map[string]any{"reason": "client_hello_parse"})
		} else {
			if emitErr := c.probe.Emit("tls", hello); emitErr != nil {
				return n, emitErr
			}
		}
		c.records, c.handshake = nil, nil
		break
	}
	return n, err
}

type cursor struct {
	data   []byte
	failed bool
}

func (c *cursor) take(n int) []byte {
	if n < 0 || n > len(c.data) {
		c.failed = true
		return nil
	}
	b := c.data[:n]
	c.data = c.data[n:]
	return b
}
func (c *cursor) byte() int {
	b := c.take(1)
	if b == nil {
		return 0
	}
	return int(b[0])
}
func (c *cursor) word() int {
	b := c.take(2)
	if b == nil {
		return 0
	}
	return int(binary.BigEndian.Uint16(b))
}
func (c *cursor) vector8() []byte  { return c.take(c.byte()) }
func (c *cursor) vector16() []byte { return c.take(c.word()) }

func words(data []byte) []int {
	out := make([]int, 0, len(data)/2)
	for len(data) >= 2 {
		n := int(binary.BigEndian.Uint16(data))
		data = data[2:]
		// GREASE is randomized by design; retain its position.
		if n&0x0f0f == 0x0a0a {
			n = -1
		}
		out = append(out, n)
	}
	return out
}

func clientHello(raw []byte) (map[string]any, bool) {
	if len(raw) < 4 || raw[0] != 1 {
		return nil, false
	}
	c := &cursor{data: raw[4:]}
	legacy := c.word()
	c.take(32)
	c.vector8()
	ciphers := words(c.vector16())
	compression := c.vector8()
	ext := &cursor{data: c.vector16()}
	order := make([]int, 0)
	details := make(map[int]any)
	for len(ext.data) > 0 && !ext.failed {
		id := ext.word()
		data := ext.vector16()
		if id&0x0f0f == 0x0a0a {
			id = -1
		}
		order = append(order, id)
		v := &cursor{data: data}
		switch id {
		case 10, 13:
			details[id] = words(v.vector16())
		case 11, 45:
			values := make([]int, 0)
			for _, n := range v.vector8() {
				values = append(values, int(n))
			}
			details[id] = values
		case 43:
			details[id] = words(v.vector8())
		case 16:
			a := &cursor{data: v.vector16()}
			values := make([]string, 0)
			for len(a.data) > 0 && !a.failed {
				values = append(values, string(a.vector8()))
			}
			if a.failed {
				return nil, false
			}
			details[id] = values
		case 51:
			k := &cursor{data: v.vector16()}
			groups := make([]int, 0)
			for len(k.data) > 0 && !k.failed {
				group := k.word()
				if group&0x0f0f == 0x0a0a {
					group = -1
				}
				groups = append(groups, group)
				k.vector16()
			}
			if k.failed {
				return nil, false
			}
			details[id] = groups
		}
		if v.failed {
			return nil, false
		}
	}
	if c.failed || ext.failed || len(c.data) != 0 {
		return nil, false
	}
	modes := make([]int, len(compression))
	for i, n := range compression {
		modes[i] = int(n)
	}
	return map[string]any{
		"evidence": "first_client_hello_original_tunnel", "legacy_version": legacy,
		"cipher_suites": ciphers, "compression_methods": modes,
		"extension_order": order, "extensions": details,
	}, true
}
