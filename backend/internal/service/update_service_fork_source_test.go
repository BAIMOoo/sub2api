//go:build unit

package service

import (
	"context"
	"testing"

	"github.com/stretchr/testify/require"
)

// Record the repository passed to the client, not the package constant: the
// update and rollback paths must both use fork releases, even at equal versions.
type forkReleaseSourceRecorder struct {
	updateServiceGitHubClientStub
	latestRepo string
	recentRepo string
}

func (r *forkReleaseSourceRecorder) FetchLatestRelease(ctx context.Context, repo string) (*GitHubRelease, error) {
	r.latestRepo = repo
	return r.updateServiceGitHubClientStub.FetchLatestRelease(ctx, repo)
}

func (r *forkReleaseSourceRecorder) FetchRecentReleases(ctx context.Context, repo string, limit int) ([]*GitHubRelease, error) {
	r.recentRepo = repo
	return r.updateServiceGitHubClientStub.FetchRecentReleases(ctx, repo, limit)
}

func TestUpdateServiceForkReleaseSource(t *testing.T) {
	ctx := context.Background()
	client := &forkReleaseSourceRecorder{updateServiceGitHubClientStub: updateServiceGitHubClientStub{
		release:        &GitHubRelease{TagName: "v0.2.23"},
		recentReleases: []*GitHubRelease{{TagName: "v0.2.22"}, {TagName: "v0.2.23"}},
	}}
	svc := NewUpdateService(&updateServiceCacheStub{}, client, "0.2.23", "release")
	info, err := svc.CheckUpdate(ctx, true)
	require.NoError(t, err)
	require.False(t, info.HasUpdate)
	client.release = &GitHubRelease{TagName: "v0.2.24"}
	info, err = svc.CheckUpdate(ctx, true)
	require.NoError(t, err)
	require.True(t, info.HasUpdate)
	require.Equal(t, "0.2.24", info.LatestVersion)
	versions, err := svc.ListRollbackVersions(ctx)
	require.NoError(t, err)
	require.Len(t, versions, 1)
	require.Equal(t, "0.2.22", versions[0].Version)
	require.Equal(t, "BAIMOoo/sub2api", client.latestRepo)
	require.Equal(t, "BAIMOoo/sub2api", client.recentRepo)
}
