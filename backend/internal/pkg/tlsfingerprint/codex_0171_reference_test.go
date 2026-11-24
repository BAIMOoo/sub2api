//go:build unit

package tlsfingerprint

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"io"
	"net"
	"os"
	"testing"
	"time"

	utls "github.com/refraction-networking/utls"
	"github.com/stretchr/testify/require"
	"golang.org/x/crypto/cryptobyte"
)

type tlsExtensionLength struct {
	Type   uint16 `json:"type"`
	Length int    `json:"length"`
}

func TestCodex0171ClientHelloMatchesNativeReference(t *testing.T) {
	raw, err := os.ReadFile("testdata/codex_exec_0171_linux_amd64_clienthello.json")
	require.NoError(t, err)
	var ref struct {
		SNI              string               `json:"sni"`
		LegacyVersion    uint16               `json:"legacy_version"`
		RecordVersion    uint16               `json:"tls_record_version"`
		SessionIDBytes   int                  `json:"session_id_bytes"`
		CipherSuites     []uint16             `json:"cipher_suites"`
		Compression      []uint8              `json:"compression_methods"`
		ExtensionLengths []tlsExtensionLength `json:"extension_lengths"`
		Extensions       struct {
			Groups           []utls.CurveID         `json:"10"`
			PointFormats     []uint8                `json:"11"`
			SignatureSchemes []utls.SignatureScheme `json:"13"`
			Versions         []uint16               `json:"43"`
			PSKModes         []uint8                `json:"45"`
		} `json:"extensions"`
		KeyShares []struct {
			Group utls.CurveID `json:"group"`
			Bytes int          `json:"bytes"`
		} `json:"key_share_lengths"`
	}
	require.NoError(t, json.Unmarshal(raw, &ref))

	client, capture := net.Pipe()
	t.Cleanup(func() { _ = client.Close(); _ = capture.Close() })
	deadline := time.Now().Add(5 * time.Second)
	require.NoError(t, capture.SetReadDeadline(deadline))
	ctx, cancel := context.WithDeadline(t.Context(), deadline)
	defer cancel()
	result := make(chan error, 1)
	go func() {
		_, err := performTLSHandshake(ctx, client, OpenAICodexLinuxProfile(), ref.SNI+":443")
		result <- err
	}()
	var recordHeader [5]byte
	_, err = io.ReadFull(capture, recordHeader[:])
	require.NoError(t, err)
	require.EqualValues(t, 22, recordHeader[0]) // Handshake record.
	require.Equal(t, ref.RecordVersion, binary.BigEndian.Uint16(recordHeader[1:3]))
	record := make([]byte, binary.BigEndian.Uint16(recordHeader[3:]))
	_, err = io.ReadFull(capture, record)
	require.NoError(t, err)
	require.NoError(t, capture.Close())
	require.Error(t, <-result, "capture ends at ClientHello, before negotiation")

	hello := utls.UnmarshalClientHello(record)
	require.NotNil(t, hello)
	require.Equal(t, ref.SNI, hello.ServerName)
	require.Len(t, hello.SessionId, ref.SessionIDBytes)
	require.Equal(t, ref.LegacyVersion, hello.Vers)
	require.Equal(t, ref.CipherSuites, hello.CipherSuites)
	require.Equal(t, ref.Compression, hello.CompressionMethods)
	require.Equal(t, ref.ExtensionLengths, readClientHelloExtensionLengths(t, record))
	require.Empty(t, hello.AlpnProtocols)
	require.Equal(t, ref.Extensions.Groups, hello.SupportedCurves)
	require.Equal(t, ref.Extensions.PointFormats, hello.SupportedPoints)
	require.Equal(t, ref.Extensions.SignatureSchemes, hello.SupportedSignatureAlgorithms)
	require.Equal(t, ref.Extensions.Versions, hello.SupportedVersions)
	require.Equal(t, ref.Extensions.PSKModes, hello.PskModes)
	require.Len(t, hello.KeyShares, len(ref.KeyShares))
	for i, share := range hello.KeyShares {
		require.Equal(t, ref.KeyShares[i].Group, share.Group)
		require.Len(t, share.Data, ref.KeyShares[i].Bytes)
	}
}

func readClientHelloExtensionLengths(t *testing.T, record []byte) []tlsExtensionLength {
	t.Helper()
	hello := cryptobyte.String(record)
	var ignored, ext cryptobyte.String
	require.True(t, hello.Skip(4+2+32) && // Handshake header, legacy version, random.
		hello.ReadUint8LengthPrefixed(&ignored) && // Session ID.
		hello.ReadUint16LengthPrefixed(&ignored) && // Ciphers.
		hello.ReadUint8LengthPrefixed(&ignored) && // Compression methods.
		hello.ReadUint16LengthPrefixed(&ext) && hello.Empty())
	var lengths []tlsExtensionLength
	for !ext.Empty() {
		var id uint16
		require.True(t, ext.ReadUint16(&id) && ext.ReadUint16LengthPrefixed(&ignored))
		lengths = append(lengths, tlsExtensionLength{Type: id, Length: len(ignored)})
	}
	return lengths
}
