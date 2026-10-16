package securityaudit

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

func TestMigrationPromptQuietWaitsForAcceptedEnqueueAndScanner(t *testing.T) {
	s := &PromptService{runner: &Runner{}}
	enqueue, ok := s.migrationEnqueueGate.TryEnter()
	require.True(t, ok)
	scan, ok := s.runner.migrationWorkGate.TryEnter()
	require.True(t, ok)
	done := make(chan func(), 1)
	go func() {
		resume, err := s.BeginMigrationQuiet(context.Background())
		if err != nil {
			panic(err)
		}
		done <- resume
	}()
	require.Eventually(t, func() bool {
		release, accepted := s.migrationEnqueueGate.TryEnter()
		if accepted {
			release()
		}
		return !accepted
	}, time.Second, time.Millisecond)
	enqueue()
	require.Eventually(t, func() bool {
		release, accepted := s.runner.migrationWorkGate.TryEnter()
		if accepted {
			release()
		}
		return !accepted
	}, time.Second, time.Millisecond)
	select {
	case <-done:
		t.Fatal("quiet canceled or ignored active scan")
	default:
	}
	require.EqualValues(t, 1, s.MigrationTaskStats().Active)
	scan()
	select {
	case resume := <-done:
		require.Zero(t, s.MigrationTaskStats().Active)
		resume()
		resume()
		release, accepted := s.runner.migrationWorkGate.TryEnter()
		require.True(t, accepted)
		release()
	case <-time.After(time.Second):
		t.Fatal("quiet did not observe natural scan completion")
	}
}
