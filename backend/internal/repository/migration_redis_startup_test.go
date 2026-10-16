package repository

import (
	"context"
	"net"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

func TestMigrationRedisStartupTargetNeverDialsSource(t *testing.T) {
	addresses := make(chan string, 16)
	options := &redis.Options{Addr: "127.0.0.1:16379", MinIdleConns: 1, PoolSize: 1,
		Dialer: func(ctx context.Context, network, address string) (net.Conn, error) {
			addresses <- address
			return nil, context.Canceled
		},
	}
	client, handover, err := newMigrationRedisClient(options, "127.0.0.1:26379")
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	if handover.SourceAddress() != options.Addr || handover.Status().Address != "127.0.0.1:26379" {
		t.Fatalf("source label or startup target lost: %+v", handover.Status())
	}
	select {
	case address := <-addresses:
		if address != "127.0.0.1:26379" {
			t.Fatalf("initial idle dial reached old authority: %s", address)
		}
	case <-time.After(time.Second):
		t.Fatal("idle connection dial did not run")
	}
}
