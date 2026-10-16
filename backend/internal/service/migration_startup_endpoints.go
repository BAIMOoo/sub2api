package service

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"sync"
)

// Only endpoint labels live here. Credentials and addresses remain fixed in
// the original private startup configuration. The runner atomically replaces
// the host file before switching a running client to a new authority.
type MigrationStartupEndpoints struct {
	Version  int    `json:"v"`
	Postgres string `json:"postgres"`
	Redis    string `json:"redis"`
}

var migrationStartupEndpoints struct {
	sync.Once
	value MigrationStartupEndpoints
	err   error
}

func parseMigrationStartupEndpoints(raw []byte) (MigrationStartupEndpoints, error) {
	var value MigrationStartupEndpoints
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if len(raw) > 512 || decoder.Decode(&value) != nil || value.Version != 1 ||
		(value.Postgres != "source" && value.Postgres != "target") ||
		(value.Redis != "source" && value.Redis != "target") {
		return value, errors.New("invalid migration startup endpoint selection")
	}
	var trailing any
	if decoder.Decode(&trailing) != io.EOF {
		return value, errors.New("invalid trailing startup endpoint selection")
	}
	return value, nil
}

// MigrationInitialEndpoints reads once per process, so both clients use the
// same durable checkpoint even if the runner replaces the file concurrently.
// A directory mount is required: mounting the file itself pins an old inode.
func MigrationInitialEndpoints() (MigrationStartupEndpoints, error) {
	migrationStartupEndpoints.Do(func() {
		migrationStartupEndpoints.value = MigrationStartupEndpoints{1, "source", "source"}
		path := os.Getenv("MIGRATION_ENDPOINT_STATE_FILE")
		if path == "" {
			return
		}
		file, err := os.Open(path)
		if err != nil {
			migrationStartupEndpoints.err = errors.New("cannot read migration startup endpoint selection")
			return
		}
		defer file.Close()
		raw, err := io.ReadAll(io.LimitReader(file, 513))
		if err != nil {
			migrationStartupEndpoints.err = errors.New("cannot read migration startup endpoint selection")
			return
		}
		migrationStartupEndpoints.value, migrationStartupEndpoints.err = parseMigrationStartupEndpoints(raw)
	})
	return migrationStartupEndpoints.value, migrationStartupEndpoints.err
}
