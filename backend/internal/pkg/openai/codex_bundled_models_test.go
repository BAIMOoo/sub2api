package openai

import (
	"testing"

	"github.com/stretchr/testify/require"
)

func TestBundledCodexModelMinimalClientVersions(t *testing.T) {
	versions := BundledCodexModelMinimalClientVersions()
	require.Equal(t, map[string]string{
		"gpt-6.1-sol":   "0.153.0",
		"gpt-6.1-terra": "0.153.0",
		"gpt-6.2-astra": "0.166.0",
		"gpt-6.2-sol":   "0.166.0",
	}, versions)

	// Returned map must be a defensive copy.
	versions["gpt-6.2-sol"] = "9.9.9"
	require.Equal(t, "0.166.0", BundledCodexMinimalClientVersion("gpt-6.2-sol"))
}

func TestBundledCodexMinimalClientVersionAliases(t *testing.T) {
	require.Equal(t, "0.166.0", BundledCodexMinimalClientVersion("gpt-6.2-sol"))
	require.Equal(t, "0.166.0", BundledCodexMinimalClientVersion("gpt-6.2-sol-high"))
	require.Equal(t, "0.166.0", BundledCodexMinimalClientVersion("openai/gpt_6.2_astra_max"))
	require.Equal(t, "0.153.0", BundledCodexMinimalClientVersion("gpt-6.1-sol-medium"))
	require.Equal(t, "0.153.0", BundledCodexMinimalClientVersion("gpt-6.1-terra-xhigh"))
	require.Equal(t, "", BundledCodexMinimalClientVersion("gpt-5.6-sol"))
	require.Equal(t, "", BundledCodexMinimalClientVersion(""))
}
