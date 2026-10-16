package main

import (
	"context"
	"fmt"

	"github.com/Wei-Shaw/sub2api/internal/securityaudit"
	"github.com/Wei-Shaw/sub2api/internal/service"
)

// This token makes dependency-injection construction order explicit. It proves
// registration, not quiescence; only a held writer lease proves the latter.
type migrationWriterControls struct{}

func provideMigrationWriterControls(
	timing *service.TimingWheelService,
	deferred *service.DeferredService,
	usage *service.UsageRecordWorkerPool,
	billing *service.BillingCacheService,
	audit *service.AuditLogService,
	reject *service.OpsIngressRejectAggregator,
	logs *service.OpsSystemLogSink,
	plugins *service.PluginManager,
	prompt *securityaudit.PromptService,
	promptConfig *securityaudit.ConfigManager,
	authOutbox *service.AuthCacheInvalidationWorker,
	ops *service.OpsService,
	moderation *service.ContentModerationService,
) *migrationWriterControls {
	if !service.MigrationModeEnabled() {
		return &migrationWriterControls{}
	}
	service.RegisterMigrationWriterQuiescer("timing_wheel", timing.MigrationTaskStats, timing.PauseAndWait)
	service.RegisterMigrationWriterQuiescer("scheduled_writers", func() service.MigrationTaskStats {
		if service.MigrationScheduledWritersQuiet() != nil {
			return service.MigrationTaskStats{Active: 1}
		}
		return service.MigrationTaskStats{}
	}, func(ctx context.Context) (func(), error) {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		if err := service.MigrationScheduledWritersQuiet(); err != nil {
			return nil, err
		}
		return func() {}, nil
	})
	service.RegisterMigrationWriterQuiescer("plugins", plugins.MigrationTaskStats, plugins.BeginMigrationQuiet)
	service.RegisterMigrationWriterQuiescer("prompt_audit", prompt.MigrationTaskStats, prompt.BeginMigrationQuiet)
	service.RegisterMigrationWriterQuiescer("prompt_config", promptConfig.MigrationConfigTaskStats, promptConfig.BeginMigrationConfigQuiet)
	service.RegisterMigrationWriterQuiescer("auth_outbox", authOutbox.MigrationTaskStats, authOutbox.BeginMigrationQuiet)
	service.RegisterMigrationWriterQuiescer("ops_runtime", ops.MigrationRuntimeTaskStats, ops.BeginMigrationRuntimeQuiet)
	// These consumers have no independent recurring producer. HTTP, scheduler,
	// plugin and prompt fences above must already be held by the controller.
	service.RegisterMigrationWriterQuiescer("usage_records", usage.MigrationTaskStats, func(ctx context.Context) (func(), error) {
		if err := service.MigrationIngressQuiet(); err != nil {
			return nil, err
		}
		return func() {}, usage.WaitForIdle(ctx)
	})
	service.RegisterMigrationWriterQuiescer("billing_cache", billing.MigrationTaskStats, func(ctx context.Context) (func(), error) {
		if err := service.MigrationIngressQuiet(); err != nil {
			return nil, err
		}
		return func() {}, billing.WaitForIdle(ctx)
	})
	service.RegisterMigrationWriterQuiescer("content_moderation", moderation.MigrationTaskStats, moderation.BeginMigrationQuiet)
	service.RegisterMigrationWriterQuiescer("detached_writers", service.MigrationDetachedTaskStats, func(ctx context.Context) (func(), error) {
		if err := service.MigrationIngressQuiet(); err != nil {
			return nil, err
		}
		return func() {}, service.WaitForMigrationDetachedTasks(ctx)
	})
	service.RegisterMigrationWriterQuiescer("deferred_last_used", deferred.MigrationTaskStats, func(ctx context.Context) (func(), error) {
		if err := service.MigrationIngressQuiet(); err != nil {
			return nil, err
		}
		return func() {}, deferred.FlushAndWait(ctx)
	})
	service.RegisterMigrationWriterQuiescer("audit_log", audit.MigrationTaskStats, audit.PauseAndFlush)
	service.RegisterMigrationWriterQuiescer("ops_ingress_reject", reject.MigrationTaskStats, reject.PauseAndFlush)
	service.RegisterMigrationWriterQuiescer("ops_system_log", logs.MigrationTaskStats, logs.PauseAndFlush)
	if err := service.MigrationWriterCoverageReady(); err != nil {
		panic(fmt.Sprintf("migration writer wiring: %v", err))
	}
	return &migrationWriterControls{}
}
