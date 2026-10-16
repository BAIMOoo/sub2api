package service

import (
	"context"
	"errors"
	"sync"

	"github.com/redis/go-redis/v9"
)

// MigrationRedisSubscription describes one of the application's actual PubSub
// consumers. Migration clients own its socket and require a server ACK before
// acknowledging a generation. Ordinary clients keep their existing behavior.
type MigrationRedisSubscription struct {
	Name    string
	Channel string
	Handle  func(context.Context, string)
	Ready   func()
}

type MigrationRedisSubscriptionManager interface {
	SubscribeMigration(context.Context, MigrationRedisSubscription) (<-chan error, error)
}

var migrationRedisSubscriptionManagers sync.Map // *redis.Client -> manager

func RegisterMigrationRedisSubscriptionManager(client *redis.Client, manager MigrationRedisSubscriptionManager) error {
	if client == nil || manager == nil {
		return errors.New("migration Redis subscription manager requires a client and manager")
	}
	if _, loaded := migrationRedisSubscriptionManagers.LoadOrStore(client, manager); loaded {
		return errors.New("migration Redis subscription manager already registered")
	}
	return nil
}

func UnregisterMigrationRedisSubscriptionManager(client *redis.Client) {
	migrationRedisSubscriptionManagers.Delete(client)
}

// StartMigrationRedisSubscription returns handled=false for an ordinary client.
// On a migration client registration and the first real subscribe ACK complete
// synchronously; done closes only when the consumer's lifetime ends.
func StartMigrationRedisSubscription(ctx context.Context, client *redis.Client, spec MigrationRedisSubscription) (handled bool, done <-chan error, err error) {
	value, ok := migrationRedisSubscriptionManagers.Load(client)
	if !ok {
		return false, nil, nil
	}
	done, err = value.(MigrationRedisSubscriptionManager).SubscribeMigration(ctx, spec)
	return true, done, err
}
