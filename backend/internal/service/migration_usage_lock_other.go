//go:build !darwin && !dragonfly && !freebsd && !linux && !netbsd && !openbsd

package service

import (
	"errors"
	"os"
)

func lockMigrationUsageJournal(string) (*os.File, error) {
	return nil, errors.New("migration usage journal requires supported filesystem locking")
}
