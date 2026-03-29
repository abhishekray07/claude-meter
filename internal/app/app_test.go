package app

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
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

func newTestApp(rw rawWriter, nw normalizedWriter, norm normalizer, logOutput io.Writer) *App {
	ch := make(chan capture.CompletedExchange, 8)
	a := &App{
		exchanges:        ch,
		rawWriter:        rw,
		normalizedWriter: nw,
		normalizer:       norm,
		logOutput:        logOutput,
		statusInterval:   100,
		modelCounts:      make(map[string]uint64),
		lastUtil:         make(map[string]float64),
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

	a := newTestApp(rw, nw, norm, io.Discard)

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

	a := newTestApp(rw, nw, norm, io.Discard)

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

func TestProcessExchangeLogLine2xx(t *testing.T) {
	t.Parallel()

	rw := &mockRawWriter{}
	nw := &mockNormalizedWriter{}
	norm := &mockNormalizer{
		normalizeFn: func(ex capture.CompletedExchange) normalize.Record {
			return normalize.Record{
				ID:            ex.ID,
				Status:        200,
				ResponseModel: "claude-opus-4-6",
				Usage: normalize.Usage{
					InputTokens:         1200,
					OutputTokens:        450,
					CacheReadInputTokens: 12800,
				},
				Ratelimit: normalize.Ratelimit{
					Windows: map[string]normalize.RatelimitWindow{
						"5h": {Utilization: 0.42},
						"7d": {Utilization: 0.18},
					},
				},
			}
		},
	}

	var buf bytes.Buffer
	a := newTestApp(rw, nw, norm, &buf)
	a.exchanges <- capture.CompletedExchange{ID: 1, RequestStartedAt: time.Now()}
	a.Close()

	output := buf.String()
	if !strings.Contains(output, "opus-4-6") {
		t.Errorf("log output missing model name, got: %s", output)
	}
	if !strings.Contains(output, "in:1,200") {
		t.Errorf("log output missing input tokens, got: %s", output)
	}
	if !strings.Contains(output, "5h:42%") {
		t.Errorf("log output missing 5h utilization, got: %s", output)
	}
}

func TestProcessExchangeLogLine429(t *testing.T) {
	t.Parallel()

	rw := &mockRawWriter{}
	nw := &mockNormalizedWriter{}
	norm := &mockNormalizer{
		normalizeFn: func(ex capture.CompletedExchange) normalize.Record {
			return normalize.Record{
				ID:     ex.ID,
				Status: 429,
				Ratelimit: normalize.Ratelimit{
					RetryAfterS: 342,
					Windows: map[string]normalize.RatelimitWindow{
						"5h": {Utilization: 1.0},
					},
				},
			}
		},
	}

	var buf bytes.Buffer
	a := newTestApp(rw, nw, norm, &buf)
	a.exchanges <- capture.CompletedExchange{ID: 1, RequestStartedAt: time.Now()}
	a.Close()

	output := buf.String()
	if !strings.Contains(output, "RATE LIMITED") {
		t.Errorf("log output missing RATE LIMITED, got: %s", output)
	}
	if !strings.Contains(output, "retry-after:342s") {
		t.Errorf("log output missing retry-after, got: %s", output)
	}
}

func TestProcessExchangeLogLineError(t *testing.T) {
	t.Parallel()

	rw := &mockRawWriter{}
	nw := &mockNormalizedWriter{}
	norm := &mockNormalizer{
		normalizeFn: func(ex capture.CompletedExchange) normalize.Record {
			return normalize.Record{
				ID:            ex.ID,
				Status:        502,
				ResponseModel: "claude-sonnet-4-6",
			}
		},
	}

	var buf bytes.Buffer
	a := newTestApp(rw, nw, norm, &buf)
	a.exchanges <- capture.CompletedExchange{ID: 1, RequestStartedAt: time.Now()}
	a.Close()

	output := buf.String()
	if !strings.Contains(output, "sonnet-4-6") {
		t.Errorf("log output missing model, got: %s", output)
	}
	if !strings.Contains(output, "502") {
		t.Errorf("log output missing status code, got: %s", output)
	}
}

func TestPeriodicStatusLine(t *testing.T) {
	t.Parallel()

	rw := &mockRawWriter{}
	nw := &mockNormalizedWriter{}
	norm := &mockNormalizer{
		normalizeFn: func(ex capture.CompletedExchange) normalize.Record {
			return normalize.Record{
				ID:            ex.ID,
				Status:        200,
				ResponseModel: "claude-opus-4-6",
				Usage:         normalize.Usage{InputTokens: 100, OutputTokens: 50},
				Ratelimit: normalize.Ratelimit{
					Windows: map[string]normalize.RatelimitWindow{
						"5h": {Utilization: 0.42},
					},
				},
			}
		},
	}

	var buf bytes.Buffer
	a := newTestApp(rw, nw, norm, &buf)
	a.statusInterval = 3 // print summary every 3 requests

	now := time.Now()
	for i := uint64(1); i <= 5; i++ {
		a.exchanges <- capture.CompletedExchange{ID: i, RequestStartedAt: now}
	}
	a.Close()

	output := buf.String()
	// Should contain a status line after 3 requests
	if !strings.Contains(output, "3 requests") {
		t.Errorf("expected '3 requests' in status line, got: %s", output)
	}
	if !strings.Contains(output, "opus") {
		t.Errorf("expected model name in status line, got: %s", output)
	}
}

func TestAPIStatsReturnsJSON(t *testing.T) {
	t.Parallel()

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()

	upstreamURL, _ := url.Parse(upstream.URL)
	logDir := t.TempDir()

	// Create a fake normalized JSONL file
	normDir := filepath.Join(logDir, "normalized")
	os.MkdirAll(normDir, 0755)
	record := `{"id":1,"request_timestamp":"2026-03-25T20:00:00Z","response_timestamp":"2026-03-25T20:00:01Z","status":200,"declared_plan_tier":"max_20x","response_model":"claude-opus-4-6","usage":{"input_tokens":100,"output_tokens":50},"ratelimit":{"windows":{"5h":{"utilization":0.1}}}}`
	os.WriteFile(filepath.Join(normDir, "2026-03-25.jsonl"), []byte(record+"\n"), 0644)

	// Use absolute path to analysis dir so test works from any cwd
	repoRoot := filepath.Join("..", "..", "analysis")
	absAnalysis, _ := filepath.Abs(repoRoot)
	application, err := New(Config{
		UpstreamBaseURL: upstreamURL,
		LogDir:          logDir,
		QueueSize:       4,
		AnalysisDir:     absAnalysis,
	})
	if err != nil {
		t.Fatalf("New() error = %v", err)
	}
	defer application.Close()

	req := httptest.NewRequest(http.MethodGet, "http://proxy.local/api/stats", nil)
	recorder := httptest.NewRecorder()
	application.Handler().ServeHTTP(recorder, req)

	if recorder.Code != http.StatusOK {
		t.Fatalf("GET /api/stats status = %d, want 200. Body: %s", recorder.Code, recorder.Body.String())
	}
	ct := recorder.Header().Get("Content-Type")
	if !strings.Contains(ct, "application/json") {
		t.Errorf("Content-Type = %q, want application/json", ct)
	}
}

func TestAPIStatsNotCapturedByProxy(t *testing.T) {
	t.Parallel()

	upstreamHit := false
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstreamHit = true
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()

	upstreamURL, _ := url.Parse(upstream.URL)
	logDir := t.TempDir()
	os.MkdirAll(filepath.Join(logDir, "normalized"), 0755)
	os.MkdirAll(filepath.Join(logDir, "raw"), 0755)

	application, err := New(Config{
		UpstreamBaseURL: upstreamURL,
		LogDir:          logDir,
		QueueSize:       4,
		AnalysisDir:     "analysis",
	})
	if err != nil {
		t.Fatalf("New() error = %v", err)
	}
	defer application.Close()

	req := httptest.NewRequest(http.MethodGet, "http://proxy.local/api/stats", nil)
	recorder := httptest.NewRecorder()
	application.Handler().ServeHTTP(recorder, req)

	if upstreamHit {
		t.Error("/api/stats was proxied to upstream — it should be handled locally")
	}
}

func TestDashboardServesEmbeddedHTML(t *testing.T) {
	t.Parallel()

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()

	upstreamURL, _ := url.Parse(upstream.URL)
	logDir := t.TempDir()

	application, err := New(Config{
		UpstreamBaseURL: upstreamURL,
		LogDir:          logDir,
		QueueSize:       4,
	})
	if err != nil {
		t.Fatalf("New() error = %v", err)
	}
	defer application.Close()

	req := httptest.NewRequest(http.MethodGet, "http://proxy.local/", nil)
	req.Header.Set("Accept", "text/html")
	recorder := httptest.NewRecorder()
	application.Handler().ServeHTTP(recorder, req)

	if recorder.Code != http.StatusOK {
		t.Fatalf("GET / status = %d, want 200", recorder.Code)
	}
	body := recorder.Body.String()
	if !strings.Contains(body, "claude-meter") {
		t.Error("dashboard HTML missing 'claude-meter'")
	}
	if !strings.Contains(body, "/api/stats") {
		t.Error("dashboard HTML missing /api/stats polling reference")
	}
	if !strings.Contains(body, "chart.js") {
		t.Error("dashboard HTML missing Chart.js")
	}
}
