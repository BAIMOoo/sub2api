//go:build integration

package repository

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"fmt"
	"net/url"
	"strings"
	"testing"
	"testing/fstest"
	"time"

	"github.com/Wei-Shaw/sub2api/migrations"
	"github.com/lib/pq"
	"github.com/stretchr/testify/require"
)

func TestGeminiImageMigrationInstalledForkKeepsExplicitSettings(t *testing.T) {
	content, err := migrations.FS.ReadFile(geminiImageGroupMigration)
	require.NoError(t, err)
	for _, tc := range []struct {
		name     string
		preserve bool
	}{{"fresh upstream backfill", false}, {"installed fork", true}} {
		t.Run(tc.name, func(t *testing.T) {
			ctx := context.Background()
			tx, err := integrationDB.BeginTx(ctx, nil)
			require.NoError(t, err)
			t.Cleanup(func() { _ = tx.Rollback() })
			_, err = tx.ExecContext(ctx, `CREATE TEMP TABLE groups (id bigint PRIMARY KEY, platform text, allow_image_generation boolean) ON COMMIT DROP;
INSERT INTO groups VALUES (1,'gemini',false),(2,'antigravity',false),(3,'composite',false),(4,'gemini',true),(5,'openai',false);`)
			require.NoError(t, err)
			require.NoError(t, execMigrationPreservingGroupFlags(ctx, tx, string(content), tc.preserve))
			rows, err := tx.QueryContext(ctx, "SELECT allow_image_generation FROM groups ORDER BY id")
			require.NoError(t, err)
			defer func() { _ = rows.Close() }()
			var got []bool
			for rows.Next() {
				var b bool
				require.NoError(t, rows.Scan(&b))
				got = append(got, b)
			}
			require.NoError(t, rows.Err())
			require.Equal(t, []bool{!tc.preserve, !tc.preserve, false, true, false}, got)
		})
	}
}

// A failed restore must not commit the upstream backfill or its migration ledger
// entry. Drive the real runner (including recognition of a pre-existing fork),
// not just the SQL helper, and retry against the same database.
func TestInstalledForkImageMigrationRollsBackAndRetries(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), time.Minute)
	defer cancel()

	// Use a database on this package's Testcontainers Postgres. Its public
	// schema and migration ledger must be independent of the harness database.
	name := fmt.Sprintf("fork_image_246_%d", time.Now().UnixNano())
	quoted := pq.QuoteIdentifier(name)
	_, err := integrationDB.ExecContext(ctx, "CREATE DATABASE "+quoted)
	require.NoError(t, err)
	t.Cleanup(func() {
		cleanupCtx, stop := context.WithTimeout(context.Background(), 10*time.Second)
		defer stop()
		_, dropErr := integrationDB.ExecContext(cleanupCtx, "DROP DATABASE "+quoted)
		require.NoError(t, dropErr)
	})
	dsn, err := url.Parse(integrationDSN)
	require.NoError(t, err)
	dsn.Path = "/" + name
	db, err := sql.Open("postgres", dsn.String())
	require.NoError(t, err)
	t.Cleanup(func() { _ = db.Close() })

	_, err = db.ExecContext(ctx, `CREATE TABLE accounts (id bigint PRIMARY KEY);
CREATE TABLE groups (id bigint PRIMARY KEY, platform text NOT NULL, allow_image_generation boolean NOT NULL);
INSERT INTO groups VALUES (1,'gemini',false),(2,'antigravity',false),(3,'gemini',true),(4,'composite',false);`)
	require.NoError(t, err)
	marker, err := migrations.FS.ReadFile(forkCookieStoreMigration)
	require.NoError(t, err)
	files := fstest.MapFS{forkCookieStoreMigration: {Data: marker}}
	require.NoError(t, applyMigrationsFS(ctx, db, files))
	backfill, err := migrations.FS.ReadFile(geminiImageGroupMigration)
	require.NoError(t, err)
	files[geminiImageGroupMigration] = &fstest.MapFile{Data: backfill}

	flags := func() []bool {
		t.Helper()
		var values []bool
		require.NoError(t, db.QueryRowContext(ctx, `SELECT array_agg(allow_image_generation ORDER BY id) FROM groups`).Scan(pq.Array(&values)))
		return values
	}
	const ledger = `SELECT checksum FROM schema_migrations WHERE filename=$1`
	before := []bool{false, false, true, false}
	require.Equal(t, before, flags())
	_, err = db.ExecContext(ctx, `CREATE FUNCTION fail_restore() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'injected restore failure'; END $$;
CREATE TRIGGER reject_restore BEFORE UPDATE ON groups FOR EACH ROW
WHEN (OLD.allow_image_generation AND NOT NEW.allow_image_generation) EXECUTE FUNCTION fail_restore();`)
	require.NoError(t, err)
	err = applyMigrationsFS(ctx, db, files)
	require.ErrorContains(t, err, "injected restore failure")
	require.Equal(t, before, flags())
	var checksum string
	require.ErrorIs(t, db.QueryRowContext(ctx, ledger, geminiImageGroupMigration).Scan(&checksum), sql.ErrNoRows)

	_, err = db.ExecContext(ctx, `DROP TRIGGER reject_restore ON groups`)
	require.NoError(t, err)
	require.NoError(t, applyMigrationsFS(ctx, db, files))
	require.Equal(t, before, flags())
	require.NoError(t, db.QueryRowContext(ctx, ledger, geminiImageGroupMigration).Scan(&checksum))
	require.Equal(t, fmt.Sprintf("%x", sha256.Sum256([]byte(strings.TrimSpace(string(backfill))))), checksum)

	// A later administrator decision is authoritative on repeated startup.
	_, err = db.ExecContext(ctx, `UPDATE groups SET allow_image_generation=false WHERE id=3`)
	require.NoError(t, err)
	require.NoError(t, applyMigrationsFS(ctx, db, files))
	require.Equal(t, []bool{false, false, false, false}, flags())
}
