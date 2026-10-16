package service

import (
	"errors"
	"fmt"
	"sort"
)

type MigrationBackgroundActivationSnapshot struct {
	Activated  bool              `json:"activated"`
	Activating bool              `json:"activating"`
	Started    []string          `json:"started"`
	Pending    []string          `json:"pending"`
	Failures   map[string]string `json:"failures,omitempty"`
}

func MigrationBackgroundActivationStatus() MigrationBackgroundActivationSnapshot {
	migrationStartedBackgroundWorkers.Lock()
	defer migrationStartedBackgroundWorkers.Unlock()
	status := MigrationBackgroundActivationSnapshot{
		Activated:  migrationStartedBackgroundWorkers.activated,
		Activating: migrationStartedBackgroundWorkers.activating,
		Started:    make([]string, 0, len(migrationStartedBackgroundWorkers.names)),
		Pending:    make([]string, 0, len(migrationStartedBackgroundWorkers.deferred)),
	}
	for name := range migrationStartedBackgroundWorkers.names {
		status.Started = append(status.Started, name)
	}
	for name := range migrationStartedBackgroundWorkers.deferred {
		status.Pending = append(status.Pending, name)
	}
	if len(migrationStartedBackgroundWorkers.failures) > 0 {
		status.Failures = make(map[string]string, len(migrationStartedBackgroundWorkers.failures))
		for name, failure := range migrationStartedBackgroundWorkers.failures {
			status.Failures[name] = failure
		}
	}
	sort.Strings(status.Started)
	sort.Strings(status.Pending)
	return status
}

// ActivateMigrationBackgroundWorkers restores startup-suppressed producers once
// the controller has verified the new data authority and stopped the old owner.
// Activation permanently invalidates quiet handover in this process. A callback
// is attempted at most once, even if it panics after partially starting work.
func ActivateMigrationBackgroundWorkers() error {
	if !MigrationModeEnabled() {
		return errors.New("background activation requires migration mode")
	}
	// This lock order also excludes a quiet lease that is still being acquired.
	migrationWriterRegistry.Lock()
	if migrationWriterRegistry.leased {
		migrationWriterRegistry.Unlock()
		return errors.New("background activation requires the writer quiet lease to be released")
	}
	migrationStartedBackgroundWorkers.Lock()
	if migrationStartedBackgroundWorkers.activating {
		migrationStartedBackgroundWorkers.Unlock()
		migrationWriterRegistry.Unlock()
		return errors.New("background activation is already in progress")
	}
	migrationStartedBackgroundWorkers.activated = true
	migrationStartedBackgroundWorkers.activating = true
	names := make([]string, 0, len(migrationStartedBackgroundWorkers.deferred))
	for name := range migrationStartedBackgroundWorkers.deferred {
		names = append(names, name)
	}
	sort.Strings(names)
	migrationStartedBackgroundWorkers.Unlock()
	migrationWriterRegistry.Unlock()
	defer func() {
		migrationStartedBackgroundWorkers.Lock()
		migrationStartedBackgroundWorkers.activating = false
		migrationStartedBackgroundWorkers.Unlock()
	}()
	for _, name := range names {
		migrationStartedBackgroundWorkers.Lock()
		start := migrationStartedBackgroundWorkers.deferred[name]
		delete(migrationStartedBackgroundWorkers.deferred, name)
		migrationStartedBackgroundWorkers.names[name] = true
		migrationStartedBackgroundWorkers.Unlock()
		if err := migrationStartBackgroundCallback(start); err != nil {
			migrationStartedBackgroundWorkers.Lock()
			migrationStartedBackgroundWorkers.failures[name] = err.Error()
			migrationStartedBackgroundWorkers.Unlock()
			return fmt.Errorf("background worker %s activation failed: %w", name, err)
		}
	}
	status := MigrationBackgroundActivationStatus()
	if len(status.Failures) != 0 {
		return fmt.Errorf("background activation has earlier failed starts: %v", status.Failures)
	}
	return nil
}

func migrationStartBackgroundCallback(start func()) (err error) {
	defer func() {
		if value := recover(); value != nil {
			err = fmt.Errorf("start callback panicked: %v", value)
		}
	}()
	start()
	return nil
}
