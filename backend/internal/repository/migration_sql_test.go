package repository

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"io"
	"sync"
	"testing"
	"time"
)

type migrationTestSQLConnector struct {
	name  string
	mu    sync.Mutex
	opens int
}

func (c *migrationTestSQLConnector) Driver() driver.Driver { return migrationTestSQLDriver{} }
func (c *migrationTestSQLConnector) Connect(context.Context) (driver.Conn, error) {
	c.mu.Lock()
	c.opens++
	c.mu.Unlock()
	return &migrationTestSQLConn{name: c.name}, nil
}

type migrationTestSQLDriver struct{}

func (migrationTestSQLDriver) Open(string) (driver.Conn, error) { panic("connector required") }

type migrationTestSQLConn struct{ name string }

func (c *migrationTestSQLConn) Prepare(string) (driver.Stmt, error) {
	return &migrationTestSQLStmt{name: c.name}, nil
}
func (*migrationTestSQLConn) Close() error              { return nil }
func (*migrationTestSQLConn) Begin() (driver.Tx, error) { return migrationTestSQLTx{}, nil }
func (c *migrationTestSQLConn) QueryContext(context.Context, string, []driver.NamedValue) (driver.Rows, error) {
	return &migrationTestSQLRows{name: c.name}, nil
}
func (*migrationTestSQLConn) ExecContext(context.Context, string, []driver.NamedValue) (driver.Result, error) {
	return driver.RowsAffected(1), nil
}

type migrationTestSQLTx struct{}

func (migrationTestSQLTx) Commit() error   { return nil }
func (migrationTestSQLTx) Rollback() error { return nil }

type migrationTestSQLStmt struct{ name string }

func (*migrationTestSQLStmt) Close() error  { return nil }
func (*migrationTestSQLStmt) NumInput() int { return -1 }
func (*migrationTestSQLStmt) Exec([]driver.Value) (driver.Result, error) {
	return driver.RowsAffected(1), nil
}
func (s *migrationTestSQLStmt) Query([]driver.Value) (driver.Rows, error) {
	return &migrationTestSQLRows{name: s.name}, nil
}

type migrationTestSQLRows struct {
	name string
	sent bool
}

func (*migrationTestSQLRows) Columns() []string { return []string{"endpoint"} }
func (*migrationTestSQLRows) Close() error      { return nil }
func (r *migrationTestSQLRows) Next(values []driver.Value) error {
	if r.sent {
		return io.EOF
	}
	r.sent = true
	values[0] = r.name
	return nil
}

func newMigrationTestSQLDB(t *testing.T) (*migrationSQLConnector, *sql.DB) {
	t.Helper()
	c := newMigrationSQLConnector(&migrationTestSQLConnector{name: "source"}, &migrationTestSQLConnector{name: "target"})
	db := sql.OpenDB(c)
	t.Cleanup(func() { c.barrier.resume(); _ = db.Close() })
	return c, db
}

func TestMigrationSQLRetainsCheckoutUntilLifecycleEnds(t *testing.T) {
	for _, kind := range []string{"rows", "transaction", "pinned-session"} {
		t.Run(kind, func(t *testing.T) {
			c, db := newMigrationTestSQLDB(t)
			var finish func() error
			switch kind {
			case "rows":
				rows, err := db.QueryContext(context.Background(), "select endpoint")
				if err != nil {
					t.Fatal(err)
				}
				finish = rows.Close
			case "transaction":
				tx, err := db.BeginTx(context.Background(), nil)
				if err != nil {
					t.Fatal(err)
				}
				finish = tx.Commit
			case "pinned-session":
				conn, err := db.Conn(context.Background())
				if err != nil {
					t.Fatal(err)
				}
				finish = conn.Close
			}
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
			defer cancel()
			if err := c.pause(ctx, db); err == nil {
				t.Fatal("paused while session was still checked out")
			}
			if err := c.switchTo("target"); err == nil {
				t.Fatal("switched with live session")
			}
			if err := finish(); err != nil {
				t.Fatal(err)
			}
			if err := c.switchTo("target"); err != nil {
				t.Fatal(err)
			}
			c.barrier.resume()
			var name string
			if err := db.QueryRowContext(context.Background(), "select endpoint").Scan(&name); err != nil {
				t.Fatal(err)
			}
			if name != "target" {
				t.Fatalf("query went to %q", name)
			}
			if s := c.barrier.status(); s.Active != 0 {
				t.Fatalf("checkout leaked: %+v", s)
			}
		})
	}
}

func TestMigrationSQLPreparedStatementReopensOnNewEndpoint(t *testing.T) {
	c, db := newMigrationTestSQLDB(t)
	stmt, err := db.PrepareContext(context.Background(), "select endpoint")
	if err != nil {
		t.Fatal(err)
	}
	defer stmt.Close()
	var name string
	if err := stmt.QueryRowContext(context.Background()).Scan(&name); err != nil {
		t.Fatal(err)
	}
	if name != "source" {
		t.Fatal(name)
	}
	if err := c.pause(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	if err := c.switchTo("target"); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- stmt.QueryRowContext(context.Background()).Scan(&name) }()
	select {
	case err := <-done:
		t.Fatalf("query was admitted before resume: %v", err)
	case <-time.After(10 * time.Millisecond):
	}
	c.barrier.resume()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("query remained blocked")
	}
	if name != "target" {
		t.Fatalf("prepared statement used stale endpoint %q", name)
	}
}

func TestMigrationSQLReusedPinnedSessionIsCountedBetweenStatements(t *testing.T) {
	c, db := newMigrationTestSQLDB(t)
	if _, err := db.ExecContext(context.Background(), "one"); err != nil {
		t.Fatal(err)
	}
	conn, err := db.Conn(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := conn.ExecContext(context.Background(), "lock"); err != nil {
		t.Fatal(err)
	}
	if s := c.barrier.status(); s.Active != 1 {
		t.Fatalf("session not held between operations: %+v", s)
	}
	if err := conn.Close(); err != nil {
		t.Fatal(err)
	}
	if s := c.barrier.status(); s.Active != 0 {
		t.Fatalf("session not returned: %+v", s)
	}
}
