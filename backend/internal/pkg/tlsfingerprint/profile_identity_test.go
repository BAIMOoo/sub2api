//go:build unit

package tlsfingerprint

import (
	"testing"

	"github.com/stretchr/testify/require"
)

// Profile.Identity 是连接池缓存键的一部分：同一形态要稳定，形态任何一项变了都要变。
func TestProfileIdentityStableAndSensitive(t *testing.T) {
	base := OpenAICodexLinuxProfile()
	same := OpenAICodexLinuxProfile()
	require.NotEmpty(t, base.Identity())
	require.Equal(t, base.Identity(), same.Identity(), "同一形态的两次构造必须得到同一身份")

	noCompression := OpenAICodexLinuxProfile()
	noCompression.DisableCompression = false
	require.NotEqual(t, base.Identity(), noCompression.Identity(), "压缩开关变化必须改变身份")

	noOrders := OpenAICodexLinuxProfile()
	noOrders.HTTPHeaderOrders = nil
	require.NotEqual(t, base.Identity(), noOrders.Identity(), "头序表变化必须改变身份")

	renamed := OpenAICodexLinuxProfile()
	renamed.Name = "renamed"
	require.NotEqual(t, base.Identity(), renamed.Identity(), "名字变化必须改变身份")

	var missing *Profile
	require.Empty(t, missing.Identity(), "nil 形态返回空身份")
}
