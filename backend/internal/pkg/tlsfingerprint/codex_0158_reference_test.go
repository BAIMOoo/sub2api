//go:build unit

package tlsfingerprint

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/require"
)

// The reference comes from the original Linux amd64 binary with no custom CA.
// This binds the runtime profile to captured arrays; dialer tests separately
// verify the profile-to-ClientHello serialization.
func TestCodex0158LinuxProfileMatchesNativeReference(t *testing.T) {
	body, err := os.ReadFile(filepath.Join("..", "..", "service", "testdata", "codex_exec_0158_linux_amd64", "native-tls", "profile.json"))
	require.NoError(t, err)
	var reference struct {
		CipherSuites   []uint16            `json:"cipher_suites"`
		ExtensionOrder []uint16            `json:"extension_order"`
		Extensions     map[string][]uint16 `json:"extensions"`
		ALPN           []string            `json:"alpn"`
		GREASE         bool                `json:"grease_present"`
	}
	require.NoError(t, json.Unmarshal(body, &reference))
	profile := OpenAICodexLinuxProfile()
	require.Equal(t, reference.CipherSuites, profile.CipherSuites)
	require.Equal(t, reference.ExtensionOrder, profile.Extensions)
	require.Equal(t, reference.Extensions["10"], profile.Curves)
	require.Equal(t, reference.Extensions["11"], profile.PointFormats)
	require.Equal(t, reference.Extensions["13"], profile.SignatureAlgorithms)
	require.Equal(t, reference.Extensions["43"], profile.SupportedVersions)
	require.Equal(t, reference.Extensions["45"], profile.PSKModes)
	require.Equal(t, reference.Extensions["51"], profile.KeyShareGroups)
	require.Empty(t, reference.ALPN)
	require.NotContains(t, profile.Extensions, uint16(16))
	require.Equal(t, reference.GREASE, profile.EnableGREASE)
}
