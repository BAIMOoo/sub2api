package repository

import (
	"compress/gzip"
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/klauspost/compress/zstd"
	"github.com/stretchr/testify/require"
)

type codexWriteFlushCloser interface {
	io.WriteCloser
	Flush() error
}

func newCodexCompressedTestWriter(w io.Writer, encoding string) (codexWriteFlushCloser, error) {
	switch encoding {
	case "gzip":
		return gzip.NewWriter(w), nil
	case "zstd":
		return zstd.NewWriter(w)
	default:
		return nil, fmt.Errorf("unsupported test encoding %q", encoding)
	}
}

func TestHTTPUpstreamCodexCompressedSSEStreamsWithoutBufferingStallOrLeak(t *testing.T) {
	const (
		firstEvent  = "event: response.created\ndata: {\"type\":\"response.created\",\"response\":{\"id\":\"resp_stream\"}}\n\n"
		secondEvent = "event: response.completed\ndata: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp_stream\",\"status\":\"completed\"}}\n\n"
	)
	for _, encoding := range []string{"gzip", "zstd"} {
		t.Run("complete/"+encoding, func(t *testing.T) {
			releaseSecond := make(chan struct{})
			gotAcceptEncoding := make(chan string, 1)
			handlerErrCh := make(chan error, 1)
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				gotAcceptEncoding <- r.Header.Get("Accept-Encoding")
				w.Header().Set("Content-Type", "text/event-stream")
				w.Header().Set("Content-Encoding", encoding)
				rc := http.NewResponseController(w)
				zw, err := newCodexCompressedTestWriter(w, encoding)
				if err != nil {
					handlerErrCh <- err
					http.Error(w, err.Error(), http.StatusInternalServerError)
					return
				}
				handlerErrCh <- nil
				_, _ = io.WriteString(zw, firstEvent)
				_ = zw.Flush()
				_ = rc.Flush()
				select {
				case <-releaseSecond:
				case <-r.Context().Done():
					_ = zw.Close()
					return
				}
				_, _ = io.WriteString(zw, secondEvent)
				_ = zw.Close()
			}))
			defer srv.Close()
			defer srv.CloseClientConnections()

			s, do := lifecycleClient(t, srv, false)
			ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
			defer cancel()
			req, err := http.NewRequestWithContext(ctx, http.MethodPost, srv.URL, nil)
			require.NoError(t, err)
			req.Header.Set("Accept-Encoding", "zstd,gzip")

			resp, err := do(req)
			require.NoError(t, err)
			require.Equal(t, "zstd,gzip", <-gotAcceptEncoding)
			require.NoError(t, <-handlerErrCh)
			require.Empty(t, resp.Header.Get("Content-Encoding"))

			// First SSE event must arrive before the server emits the second event or closes the stream.
			firstCh := make(chan []byte, 1)
			errCh := make(chan error, 1)
			go func() {
				buf := make([]byte, len(firstEvent))
				_, readErr := io.ReadFull(resp.Body, buf)
				if readErr != nil {
					errCh <- readErr
					return
				}
				firstCh <- buf
			}()
			select {
			case got := <-firstCh:
				require.Equal(t, firstEvent, string(got))
			case readErr := <-errCh:
				t.Fatalf("failed reading first SSE event (%s): %v", encoding, readErr)
			case <-time.After(3 * time.Second):
				t.Fatalf("streaming SSE buffered/stalled before EOF for Content-Encoding=%s", encoding)
			}

			close(releaseSecond)
			rest, err := io.ReadAll(resp.Body)
			require.NoError(t, err)
			require.Equal(t, secondEvent, string(rest))
			require.NoError(t, resp.Body.Close())
			requireNoUpstreamInFlight(t, s)
		})

		t.Run("early_close/"+encoding, func(t *testing.T) {
			handlerErrCh := make(chan error, 1)
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "text/event-stream")
				w.Header().Set("Content-Encoding", encoding)
				rc := http.NewResponseController(w)
				zw, err := newCodexCompressedTestWriter(w, encoding)
				if err != nil {
					handlerErrCh <- err
					http.Error(w, err.Error(), http.StatusInternalServerError)
					return
				}
				handlerErrCh <- nil
				_, _ = io.WriteString(zw, firstEvent)
				_ = zw.Flush()
				_ = rc.Flush()
				<-r.Context().Done()
				_ = zw.Close()
			}))
			defer srv.Close()
			defer srv.CloseClientConnections()

			s, do := lifecycleClient(t, srv, false)
			req, err := http.NewRequestWithContext(t.Context(), http.MethodPost, srv.URL, nil)
			require.NoError(t, err)
			req.Header.Set("Accept-Encoding", "zstd,gzip")

			resp, err := do(req)
			require.NoError(t, err)
			require.NoError(t, <-handlerErrCh)
			prefix := make([]byte, len(firstEvent))
			_, err = io.ReadFull(resp.Body, prefix)
			require.NoError(t, err)
			require.Equal(t, firstEvent, string(prefix))

			readStarted := make(chan struct{})
			readDone := make(chan error, 1)
			resp.Body = &notifyReadCloser{ReadCloser: resp.Body, started: readStarted}
			go func() { _, readErr := io.Copy(io.Discard, resp.Body); readDone <- readErr }()
			<-readStarted

			closed := make(chan error, 1)
			go func() { closed <- resp.Body.Close() }()
			select {
			case err := <-closed:
				require.NoError(t, err)
			case <-time.After(5 * time.Second):
				t.Fatalf("early Close blocked for Content-Encoding=%s", encoding)
			}
			select {
			case err := <-readDone:
				require.Error(t, err)
			case <-time.After(5 * time.Second):
				t.Fatalf("reader leaked after early Close for Content-Encoding=%s", encoding)
			}
			requireNoUpstreamInFlight(t, s)
		})
	}
}

func TestHTTPUpstreamCodexCompressedErrorBodiesDecompressCleanly(t *testing.T) {
	for _, encoding := range []string{"gzip", "zstd"} {
		for _, statusCode := range []int{http.StatusBadRequest, http.StatusTooManyRequests, http.StatusBadGateway} {
			t.Run(fmt.Sprintf("%s/%d", encoding, statusCode), func(t *testing.T) {
				wantBody := fmt.Sprintf(`{"error":{"type":"upstream_error","message":"status %d detail"}}`, statusCode)
				handlerErrCh := make(chan error, 1)
				srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					w.Header().Set("Content-Type", "application/json")
					w.Header().Set("Content-Encoding", encoding)
					w.WriteHeader(statusCode)
					zw, err := newCodexCompressedTestWriter(w, encoding)
					if err != nil {
						handlerErrCh <- err
						return
					}
					handlerErrCh <- nil
					_, _ = io.WriteString(zw, wantBody)
					_ = zw.Close()
				}))
				defer srv.Close()
				defer srv.CloseClientConnections()

				s, do := lifecycleClient(t, srv, false)
				req, err := http.NewRequestWithContext(t.Context(), http.MethodPost, srv.URL, nil)
				require.NoError(t, err)
				req.Header.Set("Accept-Encoding", "zstd,gzip")

				resp, err := do(req)
				require.NoError(t, err)
				require.NoError(t, <-handlerErrCh)
				require.Equal(t, statusCode, resp.StatusCode)
				require.Empty(t, resp.Header.Get("Content-Encoding"))
				gotBody, err := io.ReadAll(resp.Body)
				require.NoError(t, err)
				require.Equal(t, wantBody, string(gotBody))
				require.NoError(t, resp.Body.Close())
				requireNoUpstreamInFlight(t, s)
			})
		}
	}
}
