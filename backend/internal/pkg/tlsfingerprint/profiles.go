package tlsfingerprint

// OpenAICodexLinuxProfile 返回官方 codex 客户端在 Linux 上的 ClientHello 形态。
//
// 参照 2026-09-28 实测：在东京机用官方 codex-cli 打本机 ClientHello 捕获监听
// （0.157.0 采 48 次、0.158.0 采 49 次），两次采集的 JA3 与逐字段完全一致，
// 即官方 Linux 客户端是 OpenSSL 栈（native-tls），形态稳定：
//
//	JA3          0b85eb0d4981e69064e40753e4f0ac5f
//	记录层版本    TLS1.0（OpenSSL 约定），legacy_version TLS1.2
//	session_id    32 字节
//	cipher        30 个
//	扩展          11 个，顺序固定；**无 ALPN**、无 GREASE
//	groups        8 个（X25519MLKEM768 在首）
//	sigalg        26 个
//
// 用本仓库的 uTLS HelloCustom 拨号器可逐字段复刻（含 X25519MLKEM768 的 1216 字节
// key_share）；实测差异仅剩 SNI 取值与每次新生成的随机材料。
func OpenAICodexLinuxProfile() *Profile {
	return &Profile{
		Name: "Codex CLI (Linux, OpenSSL)",
		// 官方不发 Accept-Encoding，而 Go 的 Transport 默认会自动加 “Accept-Encoding: gzip”。
		DisableCompression: true,
		EnableGREASE:       false,
		CipherSuites: []uint16{
			0x1302, // TLS_AES_256_GCM_SHA384
			0x1303, // TLS_CHACHA20_POLY1305_SHA256
			0x1301, // TLS_AES_128_GCM_SHA256
			0xc02c, // TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384
			0xc030, // TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384
			0x009f, // TLS_DHE_RSA_WITH_AES_256_GCM_SHA384
			0xcca9, // TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256
			0xcca8, // TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305_SHA256
			0xccaa, // TLS_DHE_RSA_WITH_CHACHA20_POLY1305_SHA256
			0xc02b, // TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256
			0xc02f, // TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256
			0x009e, // TLS_DHE_RSA_WITH_AES_128_GCM_SHA256
			0xc024, // TLS_ECDHE_ECDSA_WITH_AES_256_CBC_SHA384
			0xc028, // TLS_ECDHE_RSA_WITH_AES_256_CBC_SHA384
			0x006b, // TLS_DHE_RSA_WITH_AES_256_CBC_SHA256
			0xc023, // TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA256
			0xc027, // TLS_ECDHE_RSA_WITH_AES_128_CBC_SHA256
			0x0067, // TLS_DHE_RSA_WITH_AES_128_CBC_SHA256
			0xc00a, // TLS_ECDHE_ECDSA_WITH_AES_256_CBC_SHA
			0xc014, // TLS_ECDHE_RSA_WITH_AES_256_CBC_SHA
			0x0039, // TLS_DHE_RSA_WITH_AES_256_CBC_SHA
			0xc009, // TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA
			0xc013, // TLS_ECDHE_RSA_WITH_AES_128_CBC_SHA
			0x0033, // TLS_DHE_RSA_WITH_AES_128_CBC_SHA
			0x009d, // TLS_RSA_WITH_AES_256_GCM_SHA384
			0x009c, // TLS_RSA_WITH_AES_128_GCM_SHA256
			0x003d, // TLS_RSA_WITH_AES_256_CBC_SHA256
			0x003c, // TLS_RSA_WITH_AES_128_CBC_SHA256
			0x0035, // TLS_RSA_WITH_AES_256_CBC_SHA
			0x002f, // TLS_RSA_WITH_AES_128_CBC_SHA
		},
		Curves: []uint16{
			0x11ec, // X25519MLKEM768
			0x001d, // x25519
			0x0017, // secp256r1
			0x001e, // x448
			0x0018, // secp384r1
			0x0019, // secp521r1
			0x0100, // ffdhe2048
			0x0101, // ffdhe3072
		},
		PointFormats: []uint16{0}, // uncompressed
		// 26 个签名算法，顺序照抄官方实测（OpenSSL 的默认列表，含 brainpool/eddsa/
		// rsa_pss 与尾部 SHA-1 系列）；此处按原始 ID 排列，不逐个标注名称。
		SignatureAlgorithms: []uint16{
			0x0905, 0x0906, 0x0904,
			0x0403, 0x0503, 0x0603,
			0x0807, 0x0808,
			0x081a, 0x081b, 0x081c,
			0x0809, 0x080a, 0x080b,
			0x0804, 0x0805, 0x0806,
			0x0401, 0x0501, 0x0601,
			0x0303, 0x0301, 0x0302,
			0x0402, 0x0502, 0x0602,
		},
		// 官方无 ALPN；扩展顺序里也不含 type 16，因此这里留空即“不发 ALPN 扩展”。
		ALPNProtocols:     nil,
		SupportedVersions: []uint16{0x0304, 0x0303}, // TLS1.3, TLS1.2
		KeyShareGroups:    []uint16{0x11ec, 0x001d},
		PSKModes:          []uint16{1}, // psk_dhe_ke
		Extensions: []uint16{
			0xff01, // renegotiation_info
			0x0000, // server_name
			0x000b, // ec_point_formats
			0x000a, // supported_groups
			0x0023, // session_ticket
			0x0016, // encrypt_then_mac
			0x0017, // extended_master_secret
			0x000d, // signature_algorithms
			0x002b, // supported_versions
			0x002d, // psk_key_exchange_modes
			0x0033, // key_share
		},
	}
}
