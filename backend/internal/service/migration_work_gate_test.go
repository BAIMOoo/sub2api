package service

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

func TestMigrationWorkGateRetainsAcceptedWorkAndReopensAfterTimeout(t *testing.T) {
	var g MigrationWorkGate
	release, ok := g.TryEnter()
	require.True(t, ok)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	resume, err := g.Pause(ctx)
	require.ErrorIs(t, err, context.Canceled)
	require.NotNil(t, resume)
	_, ok = g.TryEnter()
	require.False(t, ok)
	require.EqualValues(t, 1, g.Stats().Active)
	release()
	release()
	require.Zero(t, g.Stats().Active)
	resume()
	resume()
	release, ok = g.TryEnter()
	require.True(t, ok)
	release()
}

func TestMigrationWorkGateWaitsWithoutCancelingAcceptedJob(t *testing.T) {
	var g MigrationWorkGate
	release, ok := g.TryEnter()
	require.True(t, ok)
	ready := make(chan func(), 1)
	go func() {
		resume, err := g.Pause(context.Background())
		if err != nil {
			panic(err)
		}
		ready <- resume
	}()
	require.Eventually(t, func() bool {
		next, accepted := g.TryEnter()
		if accepted {
			next()
		}
		return !accepted
	}, time.Second, time.Millisecond)
	select {
	case <-ready:
		t.Fatal("pause returned before accepted work finished")
	default:
	}
	release()
	select {
	case resume := <-ready:
		resume()
	case <-time.After(time.Second):
		t.Fatal("pause did not observe completion")
	}
}

func TestMigrationPluginQuietRefusesIndependentActiveRuntime(t *testing.T) {
	m := &PluginManager{runtimes: map[int64]*pluginRuntime{1: {}}}
	resume, err := m.BeginMigrationQuiet(context.Background())
	require.ErrorContains(t, err, "host RPC")
	require.EqualValues(t, 1, m.MigrationTaskStats().Pending)
	_, ok := m.migrationWorkGate.TryEnter()
	require.False(t, ok)
	resume()
	release, ok := m.migrationWorkGate.TryEnter()
	require.True(t, ok)
	release()
	delete(m.runtimes, 1)
	resume, err = m.BeginMigrationQuiet(context.Background())
	require.NoError(t, err)
	require.Zero(t, m.MigrationTaskStats().Active)
	resume()
}

func TestMigrationIngressResumeGuardKeepsRequestsQueuedUntilDataReady(t *testing.T) {
	m := NewMigrationRuntime()
	fail := true
	m.SetIngressResumeGuard(func() error {
		if fail {
			return errors.New("data paused")
		}
		return nil
	})
	state := m.PauseIngress()
	require.Error(t, m.ResumeIngress(state.Epoch))
	require.True(t, m.Snapshot().Ingress.Paused)
	fail = false
	require.NoError(t, m.ResumeIngress(state.Epoch))
	require.False(t, m.Snapshot().Ingress.Paused)
}
