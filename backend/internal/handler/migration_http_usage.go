package handler

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	middleware2 "github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
)

type migrationHTTPUsageContextKey struct{}

// migrationHTTPUsage belongs to one serialized HTTP handler. It is never sent
// to the asynchronous usage pool and never marks global migration readiness.
type migrationHTTPUsage struct {
	gin      *gin.Context
	dst      gin.ResponseWriter
	writer   *MigrationCompletionWriter
	capture  *service.MigrationUsageCapture
	taskRan  bool
	finished bool
	err      error
	usageErr error
}

func migrationCompletionOptionsFromEnv(protocol MigrationCompletionProtocol) (MigrationCompletionOptions, error) {
	readPositive := func(name string) (int64, error) {
		value, err := strconv.ParseInt(strings.TrimSpace(os.Getenv(name)), 10, 64)
		if err != nil || value <= 0 {
			return 0, fmt.Errorf("%s requires an explicit positive integer", name)
		}
		return value, nil
	}
	memory, err := readPositive("MIGRATION_COMPLETION_MEMORY_BYTES")
	if err != nil {
		return MigrationCompletionOptions{}, err
	}
	frame, err := readPositive("MIGRATION_COMPLETION_MAX_FRAME_BYTES")
	if err != nil {
		return MigrationCompletionOptions{}, err
	}
	held, err := readPositive("MIGRATION_COMPLETION_MAX_HELD_BYTES")
	if err != nil {
		return MigrationCompletionOptions{}, err
	}
	if int64(int(memory)) != memory || int64(int(frame)) != frame {
		return MigrationCompletionOptions{}, errors.New("migration completion memory/frame limit exceeds native integer capacity")
	}
	return MigrationCompletionOptions{Protocol: protocol, MemoryBytes: int(memory), MaxFrameBytes: int(frame),
		MaxHeldBytes: held, SpoolDirectory: strings.TrimSpace(os.Getenv("MIGRATION_COMPLETION_SPOOL_DIR"))}, nil
}

// beginMigrationHTTPUsage snapshots the runtime's capture phase once for this
// request. Requests already admitted to legacy accounting retain that path.
// Runtime initialization and capture activation are separate operations.
func beginMigrationHTTPUsage(c *gin.Context, protocol MigrationCompletionProtocol, streaming, unsupported bool) (*migrationHTTPUsage, error) {
	journal, options, enabled := service.MigrationUsageHTTPConfig()
	if !enabled {
		return nil, nil
	}
	if journal == nil {
		return nil, errors.New("migration HTTP usage journal is configured but unavailable")
	}
	if unsupported || c == nil || c.Request == nil || strings.EqualFold(c.GetHeader("Upgrade"), "websocket") {
		return nil, ErrMigrationCompletionUnsupported
	}
	if !streaming {
		protocol = MigrationCompletionNonStreaming
	}
	completion, err := migrationCompletionOptionsFromEnv(protocol)
	if err != nil {
		return nil, err
	}
	return installMigrationHTTPUsage(c, journal, options, completion)
}

func prepareMigrationHTTPUsage(c *gin.Context, protocol MigrationCompletionProtocol, streaming, unsupported bool) (*migrationHTTPUsage, bool) {
	state, err := beginMigrationHTTPUsage(c, protocol, streaming, unsupported)
	if err == nil {
		return state, true
	}
	c.AbortWithStatusJSON(http.StatusServiceUnavailable, gin.H{"error": gin.H{
		"type": "api_error", "message": "Usage persistence is temporarily unavailable",
	}})
	return nil, false
}

func installMigrationHTTPUsage(c *gin.Context, journal *service.MigrationUsageJournal, options service.MigrationUsageCaptureOptions,
	completion MigrationCompletionOptions) (*migrationHTTPUsage, error) {
	if c == nil || c.Request == nil || journal == nil {
		return nil, errors.New("migration HTTP usage requires a request and journal")
	}
	writer, err := NewMigrationCompletionWriter(c.Writer, completion)
	if err != nil {
		return nil, err
	}
	ctx, capture := service.WithMigrationUsageCapture(c.Request.Context(), journal, options)
	state := &migrationHTTPUsage{gin: c, dst: c.Writer, writer: writer, capture: capture}
	c.Request = c.Request.WithContext(context.WithValue(ctx, migrationHTTPUsageContextKey{}, state))
	c.Writer = writer
	return state, nil
}

func migrationHTTPUsageFromContext(ctx context.Context) *migrationHTTPUsage {
	if ctx == nil {
		return nil
	}
	state, _ := ctx.Value(migrationHTTPUsageContextKey{}).(*migrationHTTPUsage)
	return state
}

// migrationRecordUsageResult retains the real RecordUsage error as well as its
// capture proof. A persisted first effect must not hide a later returned error.
func migrationRecordUsageResult(ctx context.Context, err error) error {
	if state := migrationHTTPUsageFromContext(ctx); state != nil && err != nil {
		state.usageErr = err
	}
	return err
}

// runMigrationHTTPUsageTask must run BEFORE wrapUsageRecordTaskContext, which
// intentionally carries only a small subset of request values into background
// work. Here the original capture/pricing/request-ID values remain attached.
func runMigrationHTTPUsageTask(parent context.Context, task service.UsageRecordTask) (handled bool) {
	state := migrationHTTPUsageFromContext(parent)
	if state == nil {
		return false
	}
	// A recovered panic still owns this request. Preserve the return value so
	// callers cannot resubmit the same task through the legacy billing path.
	handled = true
	if task == nil || state.taskRan {
		state.fail(service.ErrMigrationUsageCaptureIncomplete)
		return true
	}
	state.taskRan = true
	finishErr := state.writer.Finish()
	// Final accounting must survive a client disconnect while keeping a
	// bounded lifetime for remaining repository lookups. This does not extend
	// the contexts of ordinary Redis/DB calls waiting at a handover barrier.
	ctx, cancel := context.WithTimeout(context.WithoutCancel(parent), 10*time.Second)
	defer cancel()
	done := service.InflightReservationFromContext(parent).Acquire()
	defer done()
	defer func() {
		if recovered := recover(); recovered != nil {
			state.fail(fmt.Errorf("migration synchronous usage task panicked: %v", recovered))
		}
	}()
	task(ctx)
	if err := errors.Join(finishErr, state.usageErr, state.capture.CheckPersisted()); err != nil {
		state.fail(err)
		return true
	}
	if err := state.writer.ConfirmPersisted(); err != nil {
		state.fail(err)
		return true
	}
	state.finished = true
	return true
}

func (s *migrationHTTPUsage) fail(cause error) {
	if s == nil || s.err != nil {
		return
	}
	s.err = cause
	_ = s.writer.Abort(cause)
	s.gin.Writer = s.dst
	s.gin.Abort()
	const payload = `{"type":"error","error":{"type":"api_error","message":"Usage persistence is temporarily unavailable"}}`
	if !s.dst.Written() {
		// Gin commits its underlying writer on handler return. Explicitly write
		// the failure through that saved writer so it cannot become an empty 200.
		s.dst.Header().Del("Content-Length")
		s.dst.Header().Del("Content-Encoding")
		s.dst.Header().Set("Content-Type", "application/json")
		s.dst.WriteHeader(http.StatusServiceUnavailable)
		_, _ = s.dst.WriteString(payload)
		return
	}
	if s.writer.streaming() && !s.writer.PersistenceConfirmed() {
		// Headers/deltas already streamed cannot be retracted. Never synthesize
		// a successful terminal signal or retry an upstream operation here.
		_, _ = s.dst.WriteString("event: error\ndata: " + payload + "\n\n")
		s.dst.Flush()
	}
}

// Close is deferred by each covered handler, including all early returns and
// panic paths. Ordinary non-success responses keep their original status/body.
func (s *migrationHTTPUsage) Close() {
	if s == nil {
		return
	}
	if s.err == nil && !s.finished && s.writer.successful() {
		s.fail(service.ErrMigrationUsageCaptureIncomplete)
	}
	_ = s.writer.Close()
	s.gin.Writer = s.dst
}

func migrationHTTPGatewayUnsupported(c *gin.Context, apiKey *service.APIKey) bool {
	platform := ""
	if c != nil && c.Request != nil {
		if forced, ok := middleware2.GetForcePlatformFromContext(c); ok {
			platform = forced
		}
		if resolved, ok := service.ResolvedTargetPlatformFromContext(c.Request.Context()); ok && platform == "" {
			platform = resolved
		}
	}
	if platform == "" && apiKey != nil && apiKey.Group != nil {
		platform = apiKey.Group.Platform
	}
	return platform == service.PlatformGemini
}
