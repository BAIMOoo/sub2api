// bridge_probe is an isolated rehearsal backend. It never connects to sub2api,
// PostgreSQL, Redis, production listeners, or cloud APIs.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"regexp"
	"strconv"
	"syscall"
	"time"

	"github.com/coder/websocket"
)

func probeHandler(identity string) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /identity", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-Probe-Identity", identity)
		_ = json.NewEncoder(w).Encode(map[string]string{"identity": identity})
	})
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	})
	mux.HandleFunc("GET /sse", func(w http.ResponseWriter, r *http.Request) {
		count, err1 := strconv.Atoi(r.URL.Query().Get("count"))
		interval, err2 := strconv.Atoi(r.URL.Query().Get("interval_ms"))
		if err1 != nil || err2 != nil || count < 2 || count > 120 || interval < 10 || interval > 1000 || count*interval > 60000 {
			http.Error(w, "invalid bounded rehearsal stream", http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-Probe-Identity", identity)
		for sequence := 0; sequence < count; sequence++ {
			payload, _ := json.Marshal(map[string]any{"identity": identity, "sequence": sequence})
			if _, err := fmt.Fprintf(w, "event: probe\ndata: %s\n\n", payload); err != nil {
				return
			}
			w.(http.Flusher).Flush()
			if sequence != count-1 {
				select {
				case <-r.Context().Done():
					return
				case <-time.After(time.Duration(interval) * time.Millisecond):
				}
			}
		}
		payload, _ := json.Marshal(map[string]any{"identity": identity, "count": count})
		_, _ = fmt.Fprintf(w, "event: probe_done\ndata: %s\n\n", payload)
		w.(http.Flusher).Flush()
	})
	mux.HandleFunc("GET /ws", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Probe-Identity", identity)
		conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{CompressionMode: websocket.CompressionDisabled})
		if err != nil {
			return
		}
		defer conn.CloseNow()
		conn.SetReadLimit(64 * 1024)
		for {
			ctx, cancel := context.WithTimeout(r.Context(), 60*time.Second)
			kind, payload, err := conn.Read(ctx)
			if err == nil {
				err = conn.Write(ctx, kind, payload)
			}
			cancel()
			if err != nil {
				return
			}
		}
	})
	return mux
}

func main() {
	identity := flag.String("identity", "", "source-<run id> or target-<run id>")
	listen := flag.String("listen", "0.0.0.0:8080", "container-only rehearsal listener")
	flag.Parse()
	if *listen != "0.0.0.0:8080" || !regexp.MustCompile(`^(source|target)-[a-f0-9]{16}$`).MatchString(*identity) {
		log.Fatal("refuse a non-rehearsal identity or listener")
	}
	server := &http.Server{Addr: *listen, Handler: probeHandler(*identity), ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 16 * 1024}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	go func() {
		<-ctx.Done()
		shutdown, done := context.WithTimeout(context.Background(), 5*time.Second)
		defer done()
		_ = server.Shutdown(shutdown)
	}()
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
}
