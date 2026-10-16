package repository

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestMigrationSQLConfigDefaultsAndRejectsUncontrolledEnable(t *testing.T) {
	t.Setenv("MIGRATION_POSTGRES_ENABLED", "")
	t.Setenv("MIGRATION_RUNTIME_ADDR", "")
	source := &migrationTestSQLConnector{name: "source"}
	if c, err := initMigrationSQL(source); err != nil || c != nil {
		t.Fatalf("default config = %v %v", c, err)
	}
	t.Setenv("MIGRATION_POSTGRES_ENABLED", "true")
	if _, err := initMigrationSQL(source); err == nil {
		t.Fatal("enabled without private control listener")
	}
	t.Setenv("MIGRATION_RUNTIME_ADDR", "127.0.0.1:18741")
	t.Setenv("MIGRATION_RUNTIME_TOKEN", strings.Repeat("x", 32))
	t.Setenv("MIGRATION_POSTGRES_TARGET_DSN", "")
	if _, err := initMigrationSQL(source); err == nil {
		t.Fatal("enabled without fixed target DSN")
	}
	t.Setenv("MIGRATION_POSTGRES_TARGET_DSN", "host=127.0.0.1 port=5433 user=isolated dbname=isolated sslmode=disable")
	if c, err := initMigrationSQL(source); err != nil || c == nil {
		t.Fatalf("valid isolated config = %v %v", c, err)
	}
	t.Setenv("MIGRATION_POSTGRES_ENABLED", "maybe")
	if _, err := initMigrationSQL(source); err == nil {
		t.Fatal("invalid opt-in accepted")
	}
}

func TestMigrationSQLControlRequiresClosedAdmissionAndMatchingEpoch(t *testing.T) {
	c, db := newMigrationTestSQLDB(t)
	control := &migrationSQLControl{connector: c, db: db, maxIdle: 2}
	for _, body := range []string{
		`{"endpoint":"target","expected_epoch":0}`,
		`{"endpoint":"target"}`,
		`{"endpoint":"target","expected_epoch":0,"dsn":"untrusted"}`,
	} {
		r := httptest.NewRequest(http.MethodPost, "/data/postgres/switch", strings.NewReader(body))
		w := httptest.NewRecorder()
		control.ServeHTTP(w, r)
		if w.Code < 400 {
			t.Fatalf("switch accepted before valid pause: %s", body)
		}
		if control.snapshot().Endpoint != "source" {
			t.Fatal("endpoint changed")
		}
	}
	if err := c.pause(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	r := httptest.NewRequest(http.MethodPost, "/data/postgres/switch", strings.NewReader(`{"endpoint":"target","expected_epoch":1}`))
	w := httptest.NewRecorder()
	control.ServeHTTP(w, r)
	if w.Code != http.StatusConflict {
		t.Fatalf("stale epoch returned %d", w.Code)
	}
	if !control.snapshot().Paused {
		t.Fatal("failed switch reopened admission")
	}
	r = httptest.NewRequest(http.MethodPost, "/data/postgres/resume", nil)
	w = httptest.NewRecorder()
	control.ServeHTTP(w, r)
	if w.Code != http.StatusOK || control.snapshot().Paused {
		t.Fatal("explicit resume failed")
	}
}

func TestMigrationSQLQuietControlRefusesUnprovenGlobalQuiet(t *testing.T) {
	t.Setenv("MIGRATION_HANDOVER_MODE", "quiet")
	c, db := newMigrationTestSQLDB(t)
	control := &migrationSQLControl{connector: c, db: db, maxIdle: 2}
	for _, op := range []string{"pause", "switch", "resume"} {
		body := `{"expected_epoch":0}`
		if op == "switch" {
			body = `{"expected_epoch":0,"endpoint":"target"}`
		}
		w := httptest.NewRecorder()
		control.ServeHTTP(w, httptest.NewRequest(http.MethodPost, "/data/postgres/"+op, strings.NewReader(body)))
		if w.Code != http.StatusConflict {
			t.Fatalf("%s accepted unproven global quiet: %d", op, w.Code)
		}
		if control.snapshot().Paused || control.snapshot().Endpoint != "source" {
			t.Fatal("unproven quiet changed data admission or endpoint")
		}
	}
}

func TestMigrationSQLTargetProofRequiresSelectedAndResumedTarget(t *testing.T) {
	c, db := newMigrationTestSQLDB(t)
	control := &migrationSQLControl{connector: c, db: db}
	if control.targetSelected() == nil {
		t.Fatal("healthy source counted as target authority")
	}
	if err := c.pause(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	if err := c.switchTo("target"); err != nil {
		t.Fatal(err)
	}
	if control.targetSelected() == nil {
		t.Fatal("paused target counted as ready authority")
	}
	c.barrier.resume()
	if err := control.targetSelected(); err != nil {
		t.Fatal(err)
	}
}
