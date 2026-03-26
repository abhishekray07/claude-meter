package app

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"claude-meter-proxy/internal/capture"
	"claude-meter-proxy/internal/normalize"
)

func TestAppWritesRawExchangeLog(t *testing.T) {
	t.Parallel()

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	}))
	defer upstream.Close()

	upstreamURL, err := url.Parse(upstream.URL)
	if err != nil {
		t.Fatalf("url.Parse() error = %v", err)
	}

	logDir := t.TempDir()
	app, err := New(Config{
		UpstreamBaseURL: upstreamURL,
		LogDir:          logDir,
		QueueSize:       4,
	})
	if err != nil {
		t.Fatalf("New() error = %v", err)
	}

	req := httptest.NewRequest(http.MethodPost, "http://proxy.local/v1/messages", strings.NewReader("hello"))
	recorder := httptest.NewRecorder()
	app.Handler().ServeHTTP(recorder, req)

	if err := app.Close(); err != nil {
		t.Fatalf("Close() error = %v", err)
	}

	matches, err := filepath.Glob(filepath.Join(logDir, "raw", "*.jsonl"))
	if err != nil {
		t.Fatalf("Glob() error = %v", err)
	}
	if len(matches) != 1 {
		t.Fatalf("expected one raw JSONL file, got %d", len(matches))
	}

	data, err := os.ReadFile(matches[0])
	if err != nil {
		t.Fatalf("ReadFile() error = %v", err)
	}
	if len(data) == 0 {
		t.Fatal("expected raw log file to be non-empty")
	}
}

func TestAppWritesNormalizedRecordLog(t *testing.T) {
	t.Parallel()

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Request-Id", "req_norm_123")
		w.Header().Set("Anthropic-Ratelimit-Unified-Status", "allowed")
		w.Header().Set("Anthropic-Ratelimit-Unified-Representative-Claim", "five_hour")
		w.Header().Set("Anthropic-Ratelimit-Unified-5h-Status", "allowed")
		w.Header().Set("Anthropic-Ratelimit-Unified-5h-Utilization", "0.12")
		w.Header().Set("Anthropic-Ratelimit-Unified-7d-Status", "allowed")
		w.Header().Set("Anthropic-Ratelimit-Unified-7d-Utilization", "0.62")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{
			"model":"claude-haiku-4-5-20251001",
			"usage":{
				"input_tokens":5,
				"output_tokens":7
			}
		}`))
	}))
	defer upstream.Close()

	upstreamURL, err := url.Parse(upstream.URL)
	if err != nil {
		t.Fatalf("url.Parse() error = %v", err)
	}

	logDir := t.TempDir()
	app, err := New(Config{
		UpstreamBaseURL: upstreamURL,
		LogDir:          logDir,
		QueueSize:       4,
		PlanTier:        "max_20x",
	})
	if err != nil {
		t.Fatalf("New() error = %v", err)
	}

	req := httptest.NewRequest(
		http.MethodPost,
		"http://proxy.local/v1/messages?beta=true",
		strings.NewReader(`{
			"model":"claude-haiku-4-5-20251001",
			"metadata":{
				"user_id":"{\"session_id\":\"session_123\"}"
			}
		}`),
	)
	req.Header.Set("Content-Type", "application/json")

	recorder := httptest.NewRecorder()
	app.Handler().ServeHTTP(recorder, req)

	if err := app.Close(); err != nil {
		t.Fatalf("Close() error = %v", err)
	}

	matches, err := filepath.Glob(filepath.Join(logDir, "normalized", "*.jsonl"))
	if err != nil {
		t.Fatalf("Glob() error = %v", err)
	}
	if len(matches) != 1 {
		t.Fatalf("expected one normalized JSONL file, got %d", len(matches))
	}

	data, err := os.ReadFile(matches[0])
	if err != nil {
		t.Fatalf("ReadFile() error = %v", err)
	}

	var got normalize.Record
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("Unmarshal() error = %v", err)
	}

	if got.DeclaredPlanTier != "max_20x" {
		t.Fatalf("DeclaredPlanTier = %q, want %q", got.DeclaredPlanTier, "max_20x")
	}
	if got.Ratelimit.Windows["5h"].Utilization != 0.12 {
		t.Fatalf("5h utilization = %v, want %v", got.Ratelimit.Windows["5h"].Utilization, 0.12)
	}
	if got.SessionID != "session_123" {
		t.Fatalf("SessionID = %q, want %q", got.SessionID, "session_123")
	}
}

// --- mock types for unit tests ---

type mockRawWriter struct {
	mu       sync.Mutex
	calls    []capture.CompletedExchange
	writeFn  func(capture.CompletedExchange) error
}

func (m *mockRawWriter) Write(ex capture.CompletedExchange) error {
	m.mu.Lock()
	m.calls = append(m.calls, ex)
	m.mu.Unlock()
	if m.writeFn != nil {
		return m.writeFn(ex)
	}
	return nil
}

type mockNormalizedWriter struct {
	mu       sync.Mutex
	calls    []normalize.Record
	writeFn  func(normalize.Record) error
}

func (m *mockNormalizedWriter) Write(rec normalize.Record) error {
	m.mu.Lock()
	m.calls = append(m.calls, rec)
	m.mu.Unlock()
	if m.writeFn != nil {
		return m.writeFn(rec)
	}
	return nil
}

type mockNormalizer struct {
	normalizeFn func(capture.CompletedExchange) normalize.Record
}

func (m *mockNormalizer) Normalize(ex capture.CompletedExchange) normalize.Record {
	if m.normalizeFn != nil {
		return m.normalizeFn(ex)
	}
	return normalize.Record{ID: ex.ID}
}

func newTestApp(rw rawWriter, nw normalizedWriter, norm normalizer) *App {
	ch := make(chan capture.CompletedExchange, 8)
	a := &App{
		exchanges:        ch,
		rawWriter:        rw,
		normalizedWriter: nw,
		normalizer:       norm,
	}
	a.startBackgroundWriter()
	return a
}

func TestBackgroundWriterLogsWriteError(t *testing.T) {
	t.Parallel()

	rw := &mockRawWriter{
		writeFn: func(ex capture.CompletedExchange) error {
			return fmt.Errorf("disk full")
		},
	}
	nw := &mockNormalizedWriter{}
	norm := &mockNormalizer{}

	a := newTestApp(rw, nw, norm)

	now := time.Now()
	a.exchanges <- capture.CompletedExchange{ID: 1, RequestStartedAt: now}
	a.exchanges <- capture.CompletedExchange{ID: 2, RequestStartedAt: now}
	a.exchanges <- capture.CompletedExchange{ID: 3, RequestStartedAt: now}

	if err := a.Close(); err != nil {
		t.Fatalf("Close() error = %v", err)
	}

	// Even though raw writer returned errors, all three exchanges should have
	// been processed (goroutine did not die).
	rw.mu.Lock()
	rawCount := len(rw.calls)
	rw.mu.Unlock()
	if rawCount != 3 {
		t.Fatalf("raw writer called %d times, want 3", rawCount)
	}

	nw.mu.Lock()
	normCount := len(nw.calls)
	nw.mu.Unlock()
	if normCount != 3 {
		t.Fatalf("normalized writer called %d times, want 3", normCount)
	}
}

func TestBackgroundWriterRecoverFromNormalizePanic(t *testing.T) {
	t.Parallel()

	rw := &mockRawWriter{}
	nw := &mockNormalizedWriter{}
	norm := &mockNormalizer{
		normalizeFn: func(ex capture.CompletedExchange) normalize.Record {
			if ex.ID == 2 {
				panic("unexpected nil pointer in normalizer")
			}
			return normalize.Record{ID: ex.ID}
		},
	}

	a := newTestApp(rw, nw, norm)

	now := time.Now()
	a.exchanges <- capture.CompletedExchange{ID: 1, RequestStartedAt: now}
	a.exchanges <- capture.CompletedExchange{ID: 2, RequestStartedAt: now} // will panic
	a.exchanges <- capture.CompletedExchange{ID: 3, RequestStartedAt: now}

	if err := a.Close(); err != nil {
		t.Fatalf("Close() error = %v", err)
	}

	// All three exchanges should reach the raw writer. Exchange 2 panics during
	// normalization, so its normalized write is skipped, but 1 and 3 succeed.
	rw.mu.Lock()
	rawCount := len(rw.calls)
	rw.mu.Unlock()
	if rawCount != 3 {
		t.Fatalf("raw writer called %d times, want 3", rawCount)
	}

	nw.mu.Lock()
	normCount := len(nw.calls)
	nw.mu.Unlock()
	if normCount != 2 {
		t.Fatalf("normalized writer called %d times, want 2", normCount)
	}
}
