package repository

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"runtime"
	"sync"
	"testing"
	"time"
)

type migrationReviewConnector struct {
	closeStarted chan struct{}
	finishClose  <-chan struct{}
	closeOnce    sync.Once
}

func (*migrationReviewConnector) Driver() driver.Driver { return migrationTestSQLDriver{} }
func (c *migrationReviewConnector) Connect(context.Context) (driver.Conn, error) {
	return &migrationReviewSQLConn{migrationTestSQLConn: &migrationTestSQLConn{name: "source"}, owner: c}, nil
}

type migrationReviewSQLConn struct {
	*migrationTestSQLConn
	owner *migrationReviewConnector
}

func (c *migrationReviewSQLConn) Close() error {
	c.owner.closeOnce.Do(func() { close(c.owner.closeStarted) })
	if c.owner.finishClose != nil {
		<-c.owner.finishClose
	}
	return nil
}

type migrationReviewNotifyContext struct {
	context.Context
	errObserved chan struct{}
	once        sync.Once
}

func (c *migrationReviewNotifyContext) Err() error {
	c.once.Do(func() { close(c.errObserved) })
	return c.Context.Err()
}

func TestMigrationSQLReviewCanceledDirectHandoffCannotBypassPause(t *testing.T) {
	source := &migrationReviewConnector{closeStarted: make(chan struct{})}
	c := newMigrationSQLConnector(source, &migrationTestSQLConnector{name: "target"})
	db := sql.OpenDB(c)
	db.SetMaxOpenConns(1)
	t.Cleanup(func() { c.barrier.resume(); _ = db.Close() })
	held, err := db.Conn(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer held.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	notifying := &migrationReviewNotifyContext{Context: ctx, errObserved: make(chan struct{})}
	type result struct {
		conn *sql.Conn
		err  error
	}
	resultCh := make(chan result, 1)
	go func() {
		conn, err := db.Conn(notifying)
		resultCh <- result{conn, err}
	}()
	deadline := time.Now().Add(time.Second)
	for db.Stats().WaitCount == 0 {
		if time.Now().After(deadline) {
			t.Fatal("second checkout did not enter the pool wait queue")
		}
		runtime.Gosched()
	}
	pauseCtx, pauseCancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer pauseCancel()
	if err := c.pause(pauseCtx, db); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("pause with a pinned session: %v", err)
	}
	if err := held.Close(); err != nil {
		t.Fatal(err)
	}
	// A correct paused validator discards the old connection. On the buggy
	// direct-handoff path ResetSession instead enters acquire with notifying.
	select {
	case <-source.closeStarted:
	case <-notifying.errObserved:
	case <-time.After(time.Second):
		t.Fatal("neither source close nor ResetSession gate was reached")
	}
	cancel()
	select {
	case got := <-resultCh:
		if got.conn != nil {
			// A new context on this pinned session would now bypass the gate.
			_, bypassErr := got.conn.ExecContext(context.Background(), "would write to source")
			_ = got.conn.Close()
			t.Fatalf("canceled paused checkout returned a usable connection; subsequent write error=%v, checkout error=%v", bypassErr, got.err)
		}
		if !errors.Is(got.err, context.Canceled) {
			t.Fatalf("checkout error = %v, want context cancellation", got.err)
		}
	case <-time.After(time.Second):
		t.Fatal("canceled checkout remained blocked")
	}
}

func TestMigrationSQLReviewIdleResetCancellationIsNotIgnoredBySQLPool(t *testing.T) {
	c, db := newMigrationTestSQLDB(t)
	if _, err := db.ExecContext(context.Background(), "warm idle connection"); err != nil {
		t.Fatal(err)
	}
	// Isolate SessionResetter's contract independently of the controller's
	// idle-pool drain. database/sql ignores reset errors other than ErrBadConn.
	if err := c.barrier.pause(context.Background()); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	notifying := &migrationReviewNotifyContext{Context: ctx, errObserved: make(chan struct{})}
	resultCh := make(chan error, 1)
	go func() {
		conn, err := db.Conn(notifying)
		if conn != nil {
			_ = conn.Close()
			resultCh <- errors.New("pool returned a connection after ResetSession admission failed")
			return
		}
		resultCh <- err
	}()
	select {
	case <-notifying.errObserved:
	case <-time.After(time.Second):
		t.Fatal("ResetSession did not attempt admission")
	}
	cancel()
	select {
	case err := <-resultCh:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("checkout cancellation was ignored: %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("canceled idle checkout remained blocked")
	}
}

func TestMigrationSQLReviewEpochSwitchSerializesAgainstResume(t *testing.T) {
	c, _ := newMigrationTestSQLDB(t)
	if err := c.barrier.pause(context.Background()); err != nil {
		t.Fatal(err)
	}
	c.mu.Lock()
	locked := true
	defer func() {
		if locked {
			c.mu.Unlock()
		}
	}()
	switched := make(chan error, 1)
	go func() { switched <- c.switchTo("target") }()
	// The switch must retain barrier.mu while waiting to update the endpoint.
	// This white-box check creates the exact old requireDrained/update gap.
	deadline := time.Now().Add(time.Second)
	for {
		if !c.barrier.mu.TryLock() {
			break
		}
		c.barrier.mu.Unlock()
		if time.Now().After(deadline) {
			t.Fatal("endpoint switch never held the admission lock while waiting for the epoch lock")
		}
		runtime.Gosched()
	}
	resumed := make(chan struct{})
	go func() { c.barrier.resume(); close(resumed) }()
	select {
	case <-resumed:
		t.Fatal("admission reopened before the endpoint update completed")
	case <-time.After(10 * time.Millisecond):
	}
	c.mu.Unlock()
	locked = false
	select {
	case err := <-switched:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("endpoint update deadlocked with resume")
	}
	select {
	case <-resumed:
	case <-time.After(time.Second):
		t.Fatal("resume stayed blocked after endpoint update")
	}
}

func TestMigrationSQLReviewPausedCloseRetainsSessionUntilUnderlyingClose(t *testing.T) {
	finishClose := make(chan struct{})
	source := &migrationReviewConnector{closeStarted: make(chan struct{}), finishClose: finishClose}
	c := newMigrationSQLConnector(source, &migrationTestSQLConnector{name: "target"})
	db := sql.OpenDB(c)
	var finishOnce sync.Once
	finish := func() { finishOnce.Do(func() { close(finishClose) }) }
	t.Cleanup(func() { finish(); c.barrier.resume(); _ = db.Close() })
	held, err := db.Conn(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	pauseCtx, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer cancel()
	if err := c.pause(pauseCtx, db); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("pause with a pinned session: %v", err)
	}
	closed := make(chan error, 1)
	go func() { closed <- held.Close() }()
	select {
	case <-source.closeStarted:
	case <-time.After(time.Second):
		t.Fatal("underlying source session was not closed")
	}
	if active := c.barrier.status().Active; active != 1 {
		t.Fatalf("reported %d active checkouts while the underlying source session still held its locks", active)
	}
	if err := c.switchTo("target"); err == nil {
		t.Fatal("switched while the underlying source session was still closing")
	}
	finish()
	select {
	case err := <-closed:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("connection close did not finish")
	}
	if err := c.switchTo("target"); err != nil {
		t.Fatal(err)
	}
}

func TestMigrationSQLReviewUnusedOpenerConnectionCannotBecomeUncountedIdle(t *testing.T) {
	c := newMigrationSQLConnector(&migrationTestSQLConnector{name: "source"}, &migrationTestSQLConnector{name: "target"})
	conn, err := c.Connect(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	// A background opener can hand an unused connection to a canceled waiter.
	// Go 1.27 returns it via putConn(..., false), calling Validator without
	// setting needReset. A later checkout would skip SessionResetter entirely.
	if conn.(driver.Validator).IsValid() {
		t.Fatal("unused opener connection was allowed back into the pool without a future SessionResetter guarantee")
	}
	if active := c.barrier.status().Active; active != 1 {
		t.Fatalf("unused source session no longer counted before close: %d", active)
	}
	if err := conn.Close(); err != nil {
		t.Fatal(err)
	}
	if active := c.barrier.status().Active; active != 0 {
		t.Fatalf("unused source session leaked after close: %d", active)
	}
}
