//go:build integration

package repository

import (
	"context"
	"database/sql"
	"errors"
	"net/url"
	"testing"
	"time"

	"github.com/lib/pq"
	"github.com/stretchr/testify/require"
	tcpostgres "github.com/testcontainers/testcontainers-go/modules/postgres"
)

// This exercises actual lib/pq and database/sql lifecycles. It does not claim
// to test physical replication, promotion, writer fencing or a 10s cutover.
func TestMigrationSQLIntegrationPinnedLocksTransactionsRowsAndStatements(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	container, err := tcpostgres.Run(ctx, selectDockerImage(ctx, postgresImageTag),
		tcpostgres.WithDatabase("migration_source"), tcpostgres.WithUsername("postgres"),
		tcpostgres.WithPassword("isolated-test-only"), tcpostgres.BasicWaitStrategies())
	require.NoError(t, err)
	t.Cleanup(func() { _ = container.Terminate(context.Background()) })
	dsn, err := container.ConnectionString(ctx, "sslmode=disable")
	require.NoError(t, err)
	admin, err := sql.Open("postgres", dsn)
	require.NoError(t, err)
	defer admin.Close()
	_, err = admin.ExecContext(ctx, "CREATE DATABASE migration_target")
	require.NoError(t, err)
	targetURL, err := url.Parse(dsn)
	require.NoError(t, err)
	targetURL.Path = "/migration_target"
	source, err := pq.NewConnector(dsn)
	require.NoError(t, err)
	target, err := pq.NewConnector(targetURL.String())
	require.NoError(t, err)
	c := newMigrationSQLConnector(source, target)
	db := sql.OpenDB(c)
	t.Cleanup(func() { c.barrier.resume(); _ = db.Close() })
	stmt, err := db.PrepareContext(ctx, "SELECT current_database()")
	require.NoError(t, err)
	defer stmt.Close()
	var database string
	require.NoError(t, stmt.QueryRowContext(ctx).Scan(&database))
	require.Equal(t, "migration_source", database)
	pinned, err := db.Conn(ctx)
	require.NoError(t, err)
	defer pinned.Close()
	_, err = pinned.ExecContext(ctx, "SELECT pg_advisory_lock(727185)")
	require.NoError(t, err)
	tx, err := db.BeginTx(ctx, nil)
	require.NoError(t, err)
	defer tx.Rollback()
	rows, err := db.QueryContext(ctx, "SELECT generate_series(1,100)")
	require.NoError(t, err)
	defer rows.Close()
	pauseCtx, pauseCancel := context.WithTimeout(ctx, 20*time.Millisecond)
	defer pauseCancel()
	require.ErrorIs(t, c.pause(pauseCtx, db), context.DeadlineExceeded)
	require.Equal(t, 3, c.barrier.status().Active)
	require.Error(t, c.switchTo("target"))
	// Admitted pinned/transactional work must finish while admission is closed.
	require.NoError(t, tx.QueryRowContext(ctx, "SELECT current_database()").Scan(&database))
	require.Equal(t, "migration_source", database)
	require.NoError(t, tx.Commit())
	require.NoError(t, rows.Close())
	require.Equal(t, 1, c.barrier.status().Active)
	_, err = pinned.ExecContext(ctx, "SELECT pg_advisory_unlock(727185)")
	require.NoError(t, err)
	require.NoError(t, pinned.Close())
	require.Zero(t, c.barrier.status().Active)
	require.NoError(t, c.switchTo("target"))
	done := make(chan error, 1)
	go func() { done <- stmt.QueryRowContext(ctx).Scan(&database) }()
	select {
	case err := <-done:
		t.Fatalf("statement ran before resume: %v", err)
	case <-time.After(20 * time.Millisecond):
	}
	c.barrier.resume()
	select {
	case err := <-done:
		require.NoError(t, err)
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
	require.Equal(t, "migration_target", database)
	require.False(t, errors.Is(ctx.Err(), context.DeadlineExceeded))
}
