package service

import (
	"errors"
	"fmt"
	"os"
	"strings"
	"sync"
)

// MigrationQuietHandoverEnabled selects the fully drained handover. An empty
// mode retains the default-off journal foundation's existing readiness contract.
func MigrationQuietHandoverEnabled() bool {
	return strings.TrimSpace(os.Getenv("MIGRATION_HANDOVER_MODE")) == "quiet"
}

func MigrationDataQuiet() error {
	if err := MigrationIngressQuiet(); err != nil {
		return err
	}
	return MigrationWritersQuiet()
}

// Private control transitions share a lock so an ingress/writer resume cannot
// race a successful quiet proof followed by a database pause or endpoint switch.
var migrationTransition sync.Mutex

func LockMigrationTransition() func() {
	migrationTransition.Lock()
	return migrationTransition.Unlock
}

var migrationPostgresGuard struct {
	sync.RWMutex
	probe func() error
}

var migrationPostgresTarget struct {
	sync.RWMutex
	probe func() error
}

// The target proof is separate from ordinary readiness: serving from the
// original primary is healthy during preparation, but cannot authorize the
// permanent restoration of background producers on the new authority.
func RegisterMigrationPostgresTargetProbe(probe func() error) {
	migrationPostgresTarget.Lock()
	defer migrationPostgresTarget.Unlock()
	if probe == nil || migrationPostgresTarget.probe != nil {
		panic("migration PostgreSQL target proof duplicated or absent")
	}
	migrationPostgresTarget.probe = probe
}

func MigrationPostgresTargetReady() error {
	migrationPostgresTarget.RLock()
	probe := migrationPostgresTarget.probe
	migrationPostgresTarget.RUnlock()
	if probe == nil {
		return errors.New("migration PostgreSQL target proof is not connected")
	}
	return probe()
}

func RegisterMigrationPostgresQuietGuard(probe func() error) {
	migrationPostgresGuard.Lock()
	defer migrationPostgresGuard.Unlock()
	if probe == nil || migrationPostgresGuard.probe != nil {
		panic("migration PostgreSQL quiet guard duplicated or absent")
	}
	migrationPostgresGuard.probe = probe
}
func MigrationPostgresQuiet() error {
	if err := MigrationDataQuiet(); err != nil {
		return err
	}
	migrationPostgresGuard.RLock()
	probe := migrationPostgresGuard.probe
	migrationPostgresGuard.RUnlock()
	if probe == nil {
		return errors.New("migration Redis-before-PostgreSQL quiet proof is not connected")
	}
	if err := probe(); err != nil {
		return fmt.Errorf("migration PostgreSQL quiet guard: %w", err)
	}
	return nil
}

// MigrationDataEndpointsReady validates only the connected data endpoints. It
// stays usable while a writer lease is held and HTTP admission is closed.
func MigrationDataEndpointsReady() error {
	migrationControlRegistry.Lock()
	pg, redis := migrationControlRegistry.readiness["postgres"], migrationControlRegistry.readiness["redis"]
	migrationControlRegistry.Unlock()
	if pg == nil || redis == nil {
		return errors.New("migration data readiness is not connected")
	}
	if err := pg(); err != nil {
		return errors.New("migration PostgreSQL endpoint is not ready")
	}
	if err := redis(); err != nil {
		return errors.New("migration Redis endpoint is not ready")
	}
	return nil
}
