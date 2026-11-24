package openai

import (
	"encoding/json"
	"maps"
	"strings"
	"sync"
)

var (
	bundledCodexMinimalClientVersionsOnce sync.Once
	bundledCodexMinimalClientVersions     map[string]string
)

func loadBundledCodexMinimalClientVersions() {
	descriptors := [][]byte{
		CodexGPT61SolMetadata,
		CodexGPT61TerraMetadata,
		CodexGPT62SolMetadata,
		CodexGPT62AstraMetadata,
	}
	versions := make(map[string]string, len(descriptors))
	for _, raw := range descriptors {
		var meta struct {
			Slug                 string `json:"slug"`
			MinimalClientVersion string `json:"minimal_client_version"`
		}
		if err := json.Unmarshal(raw, &meta); err != nil {
			panic(err)
		}
		slug := strings.TrimSpace(meta.Slug)
		minVer := strings.TrimSpace(meta.MinimalClientVersion)
		if slug != "" && minVer != "" {
			versions[slug] = minVer
		}
	}
	bundledCodexMinimalClientVersions = versions
}

// BundledCodexModelMinimalClientVersions returns a copy of every embedded Codex
// model descriptor's declared minimal_client_version keyed by official model slug.
func BundledCodexModelMinimalClientVersions() map[string]string {
	bundledCodexMinimalClientVersionsOnce.Do(loadBundledCodexMinimalClientVersions)
	return maps.Clone(bundledCodexMinimalClientVersions)
}

// BundledCodexMinimalClientVersion returns the bundled minimal_client_version
// for a Codex model ID or local effort/compact alias, or empty string when none
// is declared in the embedded descriptors.
func BundledCodexMinimalClientVersion(model string) string {
	bundledCodexMinimalClientVersionsOnce.Do(loadBundledCodexMinimalClientVersions)
	canonical := CanonicalizeOpenAIModelAliasSpelling(model)
	if canonical == "" {
		return ""
	}
	if version, ok := bundledCodexMinimalClientVersions[canonical]; ok {
		return version
	}
	switch {
	case GPT62ModelID(canonical) != "":
		return bundledCodexMinimalClientVersions[GPT62ModelID(canonical)]
	case IsGPT61SolModelSpelling(canonical):
		return bundledCodexMinimalClientVersions["gpt-6.1-sol"]
	case IsGPT61TerraModelSpelling(canonical):
		return bundledCodexMinimalClientVersions["gpt-6.1-terra"]
	default:
		return ""
	}
}
