package repository

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"sync"
)

// migrationSQLConnector gates whole database/sql checkouts, not individual
// statements. Validator marks the return to the pool; ResetSession marks reuse.
// Thus rows, transactions, prepared statements and pinned advisory-lock sessions
// remain admitted until database/sql actually returns their connection.
//
// A controller must set MaxIdleConns(0) before pause. This also closes an unused
// connection opened by database/sql's background opener, which can enter the
// idle pool without an initial call to Validator. A pinned connection can block
// a pause indefinitely; that is intentional and must not be forcibly retired.
type migrationSQLConnector struct {
	barrier migrationBarrier
	mu      sync.Mutex
	ends    map[string]driver.Connector
	current string
	epoch   uint64
}

func newMigrationSQLConnector(source, target driver.Connector) *migrationSQLConnector {
	return &migrationSQLConnector{
		ends:    map[string]driver.Connector{"source": source, "target": target},
		current: "source",
	}
}

func (c *migrationSQLConnector) Driver() driver.Driver { return c.ends["source"].Driver() }

func (c *migrationSQLConnector) Connect(ctx context.Context) (driver.Conn, error) {
	lease, err := c.barrier.acquireLease(ctx)
	if err != nil {
		return nil, err
	}
	c.mu.Lock()
	base, epoch := c.ends[c.current], c.epoch
	c.mu.Unlock()
	conn, err := base.Connect(ctx)
	if err != nil {
		lease.release()
		return nil, err
	}
	return &migrationSQLConn{Conn: conn, owner: c, epoch: epoch, lease: lease}, nil
}

// switchTo only changes connector selection. The external runner owns fencing,
// replication catch-up, role validation and the decision to reopen admission.
func (c *migrationSQLConnector) switchTo(name string) error {
	return c.barrier.whileDrained(func() error {
		c.mu.Lock()
		defer c.mu.Unlock()
		if c.ends[name] == nil {
			return errors.New("unknown migration PostgreSQL endpoint")
		}
		if c.current != name {
			c.current = name
			c.epoch++
		}
		return nil
	})
}

func (c *migrationSQLConnector) pause(ctx context.Context, db *sql.DB) error {
	db.SetMaxIdleConns(0)
	return c.barrier.pause(ctx)
}

type migrationSQLConn struct {
	driver.Conn
	owner *migrationSQLConnector
	epoch uint64
	lease *migrationBarrierLease
	used  bool
}

func (c *migrationSQLConn) stale() bool {
	c.owner.mu.Lock()
	defer c.owner.mu.Unlock()
	return c.epoch != c.owner.epoch
}

func (c *migrationSQLConn) releaseCheckout() {
	if c.lease != nil {
		c.lease.release()
		c.lease = nil
	}
}

func (c *migrationSQLConn) Close() error {
	// Retain the checkout while the underlying session and its locks close.
	err := c.Conn.Close()
	c.releaseCheckout()
	return err
}

func (c *migrationSQLConn) IsValid() bool {
	if !c.used {
		// The background opener may hand a never-used connection to a waiter
		// whose context is canceled. database/sql then returns it with reset=false;
		// a later checkout would skip ResetSession and bypass this barrier.
		return false
	}
	valid := !c.stale()
	if v, ok := c.Conn.(driver.Validator); ok {
		valid = v.IsValid() && valid
	}
	if c.lease != nil && !c.lease.releaseIfOpen() {
		// With admission closed the pool must discard this session. Keep its
		// checkout counted until Close has actually released its server locks.
		return false
	}
	c.lease = nil
	return valid
}

func (c *migrationSQLConn) ResetSession(ctx context.Context) error {
	c.used = true // database/sql already marked this session needReset=true.
	if c.lease == nil {
		lease, err := c.owner.barrier.acquireLease(ctx)
		if err != nil {
			// database/sql ignores ResetSession errors other than ErrBadConn.
			// Return both so a canceled waiter never bypasses closed admission.
			return errors.Join(driver.ErrBadConn, err)
		}
		c.lease = lease
	}
	if c.stale() {
		// No SQL was sent. database/sql may safely obtain a fresh connection.
		c.releaseCheckout()
		return driver.ErrBadConn
	}
	if resetter, ok := c.Conn.(driver.SessionResetter); ok {
		if err := resetter.ResetSession(ctx); err != nil {
			// database/sql only discards ResetSession errors matching ErrBadConn.
			// Preserve the original error while ensuring the broken checkout closes.
			return errors.Join(driver.ErrBadConn, err)
		}
	}
	return nil
}

func (c *migrationSQLConn) PrepareContext(ctx context.Context, query string) (driver.Stmt, error) {
	c.used = true
	if preparer, ok := c.Conn.(driver.ConnPrepareContext); ok {
		return preparer.PrepareContext(ctx, query)
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return c.Conn.Prepare(query)
}

func (c *migrationSQLConn) BeginTx(ctx context.Context, opts driver.TxOptions) (driver.Tx, error) {
	c.used = true
	if beginner, ok := c.Conn.(driver.ConnBeginTx); ok {
		return beginner.BeginTx(ctx, opts)
	}
	if opts.Isolation != 0 || opts.ReadOnly {
		return nil, errors.New("driver does not support requested transaction options")
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return c.Conn.Begin()
}

func (c *migrationSQLConn) ExecContext(ctx context.Context, query string, args []driver.NamedValue) (driver.Result, error) {
	if execer, ok := c.Conn.(driver.ExecerContext); ok {
		c.used = true
		return execer.ExecContext(ctx, query, args)
	}
	return nil, driver.ErrSkip
}

func (c *migrationSQLConn) QueryContext(ctx context.Context, query string, args []driver.NamedValue) (driver.Rows, error) {
	if queryer, ok := c.Conn.(driver.QueryerContext); ok {
		c.used = true
		return queryer.QueryContext(ctx, query, args)
	}
	return nil, driver.ErrSkip
}

func (c *migrationSQLConn) Ping(ctx context.Context) error {
	c.used = true
	if pinger, ok := c.Conn.(driver.Pinger); ok {
		return pinger.Ping(ctx)
	}
	return ctx.Err()
}

func (c *migrationSQLConn) Prepare(query string) (driver.Stmt, error) {
	c.used = true
	return c.Conn.Prepare(query)
}

func (c *migrationSQLConn) Begin() (driver.Tx, error) {
	c.used = true
	return c.Conn.Begin()
}

func (c *migrationSQLConn) CheckNamedValue(value *driver.NamedValue) error {
	if checker, ok := c.Conn.(driver.NamedValueChecker); ok {
		return checker.CheckNamedValue(value)
	}
	return driver.ErrSkip
}
