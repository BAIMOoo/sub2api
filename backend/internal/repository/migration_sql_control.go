package repository

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/lib/pq"
)

type migrationSQLControl struct {
	opMu      sync.Mutex
	connector *migrationSQLConnector
	db        *sql.DB
	maxIdle   int
}

// initMigrationSQL is default-off. Targets are supplied privately at startup;
// no control request accepts a DSN or returns connection configuration.
func initMigrationSQL(source driver.Connector) (*migrationSQLControl, error) {
	raw := strings.TrimSpace(os.Getenv("MIGRATION_POSTGRES_ENABLED"))
	if raw == "" {
		return nil, nil
	}
	enabled, err := strconv.ParseBool(raw)
	if err != nil {
		return nil, errors.New("MIGRATION_POSTGRES_ENABLED must be a boolean")
	}
	if !enabled {
		return nil, nil
	}
	runtimeCfg, err := service.MigrationRuntimeConfigFromEnv()
	if err != nil || runtimeCfg.ListenAddress == "" {
		return nil, errors.New("migration PostgreSQL requires a valid private migration control listener")
	}
	dsn := strings.TrimSpace(os.Getenv("MIGRATION_POSTGRES_TARGET_DSN"))
	if dsn == "" {
		return nil, errors.New("MIGRATION_POSTGRES_TARGET_DSN is required")
	}
	target, err := pq.NewConnector(dsn)
	if err != nil {
		return nil, errors.New("invalid migration PostgreSQL target configuration")
	}
	initial, err := service.MigrationInitialEndpoints()
	if err != nil {
		return nil, err
	}
	connector := newMigrationSQLConnector(source, target)
	connector.current = initial.Postgres // no pool or business work exists yet
	return &migrationSQLControl{connector: connector}, nil
}

func (c *migrationSQLControl) register(db *sql.DB, maxIdle int) {
	c.db, c.maxIdle = db, maxIdle
	service.RegisterMigrationControlHandler("/data/postgres/", c)
	service.RegisterMigrationPostgresTargetProbe(c.targetSelected)
	service.RegisterMigrationReadinessProbe("postgres", func() error {
		if c.connector.barrier.status().Paused {
			return errors.New("PostgreSQL admission is paused")
		}
		ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
		defer cancel()
		var recovery bool
		if err := db.QueryRowContext(ctx, "SELECT pg_is_in_recovery()").Scan(&recovery); err != nil {
			return err
		}
		if recovery {
			return errors.New("PostgreSQL endpoint is a replica")
		}
		return nil
	})
}

func (c *migrationSQLControl) targetSelected() error {
	s := c.snapshot()
	if s.Endpoint != "target" || s.Paused {
		return errors.New("PostgreSQL has not selected the resumed target")
	}
	return nil
}

type migrationSQLControlStatus struct {
	migrationBarrierStatus
	Endpoint string `json:"endpoint"`
	Epoch    uint64 `json:"epoch"`
}

func (c *migrationSQLControl) snapshot() migrationSQLControlStatus {
	// Keep lock order barrier -> connector everywhere, including inspection.
	c.connector.barrier.mu.Lock()
	defer c.connector.barrier.mu.Unlock()
	c.connector.mu.Lock()
	defer c.connector.mu.Unlock()
	b := &c.connector.barrier
	return migrationSQLControlStatus{migrationBarrierStatus{Paused: b.paused, Active: b.active, PausedAt: b.pausedAt}, c.connector.current, c.connector.epoch}
}

func (c *migrationSQLControl) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Content-Type", "application/json")
	path := strings.TrimPrefix(r.URL.Path, "/data/postgres/")
	if path == "status" && r.Method == http.MethodGet {
		_ = json.NewEncoder(w).Encode(c.snapshot())
		return
	}
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	unlock := service.LockMigrationTransition()
	defer unlock()
	c.opMu.Lock()
	defer c.opMu.Unlock()
	if service.MigrationQuietHandoverEnabled() {
		if path == "pause" || path == "resume" {
			var request struct {
				ExpectedEpoch *uint64 `json:"expected_epoch"`
			}
			dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1024))
			dec.DisallowUnknownFields()
			if err := dec.Decode(&request); err != nil || request.ExpectedEpoch == nil {
				http.Error(w, "invalid migration PostgreSQL phase request", http.StatusBadRequest)
				return
			}
			var trailing any
			if dec.Decode(&trailing) != io.EOF {
				http.Error(w, "invalid migration PostgreSQL phase request", http.StatusBadRequest)
				return
			}
			if *request.ExpectedEpoch != c.snapshot().Epoch {
				http.Error(w, "migration PostgreSQL epoch mismatch", http.StatusConflict)
				return
			}
		}
		if err := service.MigrationPostgresQuiet(); err != nil {
			http.Error(w, "migration PostgreSQL requires closed ingress, drained writers and stopped Redis consumers", http.StatusConflict)
			return
		}
	}
	switch path {
	case "pause":
		// A timeout keeps admission closed. Role-aware recovery must explicitly
		// resume; no handler changes database roles or guesses the primary.
		ctx, cancel := context.WithTimeout(r.Context(), time.Second)
		defer cancel()
		if err := c.connector.pause(ctx, c.db); err != nil {
			w.WriteHeader(http.StatusConflict)
		}
	case "switch":
		var request struct {
			Endpoint      string  `json:"endpoint"`
			ExpectedEpoch *uint64 `json:"expected_epoch"`
		}
		dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1024))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&request); err != nil || request.ExpectedEpoch == nil {
			http.Error(w, "invalid migration PostgreSQL switch request", http.StatusBadRequest)
			return
		}
		var trailing any
		if dec.Decode(&trailing) != io.EOF {
			http.Error(w, "invalid migration PostgreSQL switch request", http.StatusBadRequest)
			return
		}
		status := c.snapshot()
		if !status.Paused || status.Active != 0 || status.Epoch != *request.ExpectedEpoch {
			http.Error(w, "migration PostgreSQL phase does not match", http.StatusConflict)
			return
		}
		c.connector.mu.Lock()
		base := c.connector.ends[request.Endpoint]
		c.connector.mu.Unlock()
		if base == nil {
			http.Error(w, "unknown migration PostgreSQL endpoint", http.StatusBadRequest)
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), time.Second)
		defer cancel()
		// Probe through an independent pool; the application's pool is paused.
		probe := sql.OpenDB(base)
		defer probe.Close()
		var recovery bool
		var readOnly string
		if err := probe.QueryRowContext(ctx, "SELECT pg_is_in_recovery(), current_setting('transaction_read_only')").Scan(&recovery, &readOnly); err != nil || recovery || readOnly != "off" {
			http.Error(w, "migration PostgreSQL endpoint is not writable", http.StatusConflict)
			return
		}
		if err := c.connector.switchTo(request.Endpoint); err != nil {
			http.Error(w, "migration PostgreSQL is not drained", http.StatusConflict)
			return
		}
	case "resume":
		c.db.SetMaxIdleConns(c.maxIdle)
		c.connector.barrier.resume()
	default:
		w.WriteHeader(http.StatusNotFound)
		return
	}
	_ = json.NewEncoder(w).Encode(c.snapshot())
}
