package repository

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestMigrationBarrierDrainsAndRetainsNewOperations(t *testing.T) {
	var b migrationBarrier
	release, err := b.acquire(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	paused := make(chan error, 1)
	go func() { paused <- b.pause(context.Background()) }()
	deadline := time.After(time.Second)
	for !b.status().Paused {
		select {
		case <-deadline:
			t.Fatal("pause did not close admission")
		default:
			time.Sleep(time.Millisecond)
		}
	}
	if err := b.requireDrained(); err == nil {
		t.Fatal("accepted a switch while an operation was active")
	}
	entered := make(chan struct{})
	done := make(chan struct{})
	go func() {
		defer close(done)
		releaseNew, err := b.acquire(context.Background())
		if err == nil {
			close(entered)
			releaseNew()
		}
	}()
	select {
	case <-entered:
		t.Fatal("new operation entered during pause")
	case <-time.After(10 * time.Millisecond):
	}
	release()
	release() // Completion callbacks may be invoked twice without corrupting the count.
	select {
	case err := <-paused:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("pause did not finish after the last operation")
	}
	if err := b.requireDrained(); err != nil {
		t.Fatal(err)
	}
	select {
	case <-entered:
		t.Fatal("draining implicitly reopened admission")
	default:
	}
	b.resume()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("resume did not release the waiting operation")
	}
	if s := b.status(); s.Active != 0 || s.Paused {
		t.Fatalf("unexpected final state: %+v", s)
	}
}

func TestMigrationBarrierPauseTimeoutDoesNotReopen(t *testing.T) {
	var b migrationBarrier
	release, err := b.acquire(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := b.pause(ctx); !errors.Is(err, context.Canceled) {
		t.Fatalf("pause error = %v", err)
	}
	if !b.status().Paused {
		t.Fatal("a canceled controller request must not reopen admission")
	}
	waitCtx, waitCancel := context.WithTimeout(context.Background(), time.Millisecond)
	defer waitCancel()
	if _, err := b.acquire(waitCtx); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("blocked operation error = %v", err)
	}
	release()
	if err := b.requireDrained(); err != nil {
		t.Fatal(err)
	}
	b.resume()
	if _, err := b.acquire(ctx); !errors.Is(err, context.Canceled) {
		t.Fatalf("expired work was admitted: %v", err)
	}
}
