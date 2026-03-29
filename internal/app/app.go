package app

import (
	"bytes"
	_ "embed"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"claude-meter-proxy/internal/capture"
	"claude-meter-proxy/internal/normalize"
	"claude-meter-proxy/internal/proxy"
	"claude-meter-proxy/internal/storage"
)

//go:embed dashboard.html
var dashboardHTML []byte

type rawWriter interface {
	Write(capture.CompletedExchange) error
}

type normalizedWriter interface {
	Write(normalize.Record) error
}

type normalizer interface {
	Normalize(capture.CompletedExchange) normalize.Record
}

type Config struct {
	UpstreamBaseURL *url.URL
	LogDir          string
	QueueSize       int
	PlanTier        string
	Client          *http.Client
	AnalysisDir     string
	StatusInterval  int // print status summary every N requests (0 = disabled)
}

type App struct {
	proxy            *proxy.Server
	exchanges        chan capture.CompletedExchange
	rawWriter        rawWriter
	normalizedWriter normalizedWriter
	normalizer       normalizer
	logDir           string
	analysisDir      string
	logOutput        io.Writer // where CLI log lines go (default: os.Stderr)

	statusInterval uint64
	requestCount   uint64
	modelCounts    map[string]uint64
	lastUtil       map[string]float64
	mu             sync.Mutex

	statsCache     []byte
	statsCacheTime time.Time
	statsMu        sync.Mutex

	closeOnce sync.Once
	wg        sync.WaitGroup
}

func New(cfg Config) (*App, error) {
	if cfg.UpstreamBaseURL == nil {
		return nil, fmt.Errorf("upstream base URL is required")
	}
	if cfg.LogDir == "" {
		return nil, fmt.Errorf("log dir is required")
	}
	if cfg.QueueSize <= 0 {
		cfg.QueueSize = 256
	}

	rw, err := storage.NewRawExchangeWriter(filepath.Join(cfg.LogDir, "raw"))
	if err != nil {
		return nil, err
	}
	nw, err := storage.NewNormalizedRecordWriter(filepath.Join(cfg.LogDir, "normalized"))
	if err != nil {
		return nil, err
	}
	norm := normalize.New(cfg.PlanTier)

	if cfg.AnalysisDir != "" {
		script := filepath.Join(cfg.AnalysisDir, "dashboard.py")
		if _, err := os.Stat(script); err != nil {
			log.Printf("claude-meter: dashboard script not found at %s, web dashboard disabled", script)
			cfg.AnalysisDir = ""
		}
	}

	exchanges := make(chan capture.CompletedExchange, cfg.QueueSize)
	statusInterval := uint64(cfg.StatusInterval) // 0 = disabled, flag default is 100

	app := &App{
		exchanges:        exchanges,
		rawWriter:        rw,
		normalizedWriter: nw,
		normalizer:       norm,
		logDir:           cfg.LogDir,
		analysisDir:      cfg.AnalysisDir,
		logOutput:        os.Stderr,
		statusInterval:   statusInterval,
		modelCounts:      make(map[string]uint64),
		lastUtil:         make(map[string]float64),
	}

	app.proxy = proxy.New(proxy.Config{
		UpstreamBaseURL: cfg.UpstreamBaseURL,
		Client:          cfg.Client,
		CaptureCh:       exchanges,
	})

	app.startBackgroundWriter()

	return app, nil
}

func (a *App) startBackgroundWriter() {
	a.wg.Add(1)
	go func() {
		defer a.wg.Done()
		for ex := range a.exchanges {
			a.processExchange(ex)
		}
	}()
}

func (a *App) processExchange(ex capture.CompletedExchange) {
	defer func() {
		if r := recover(); r != nil {
			log.Printf("claude-meter: recovered panic processing exchange %d: %v", ex.ID, r)
		}
	}()

	if err := a.rawWriter.Write(ex); err != nil {
		log.Printf("claude-meter: raw write error for exchange %d: %v", ex.ID, err)
	}
	rec := a.normalizer.Normalize(ex)
	if err := a.normalizedWriter.Write(rec); err != nil {
		log.Printf("claude-meter: normalized write error for exchange %d: %v", ex.ID, err)
	}
	a.logExchange(rec)
	a.trackAndMaybePrintStatus(rec)
}

func effectiveModel(rec normalize.Record) string {
	if rec.ResponseModel != "" {
		return rec.ResponseModel
	}
	return rec.RequestModel
}

func formatUtilWindows(windows map[string]normalize.RatelimitWindow) []string {
	names := make([]string, 0, len(windows))
	for name := range windows {
		names = append(names, name)
	}
	sort.Strings(names)
	var parts []string
	for _, name := range names {
		w := windows[name]
		pct := int(w.Utilization * 100)
		parts = append(parts, colorize(fmt.Sprintf("%s:%d%%", name, pct), utilizationColor(w.Utilization)))
	}
	return parts
}

func (a *App) logExchange(rec normalize.Record) {
	switch {
	case rec.Status == 429:
		// Rate limited
		parts := []string{colorize("RATE LIMITED", colorRed)}
		parts = append(parts, formatUtilWindows(rec.Ratelimit.Windows)...)
		if rec.Ratelimit.RetryAfterS > 0 {
			parts = append(parts, fmt.Sprintf("retry-after:%ds", rec.Ratelimit.RetryAfterS))
		}
		fmt.Fprintf(a.logOutput, "[claude-meter] %s\n", strings.Join(parts, " | "))

	case rec.Status >= 200 && rec.Status < 300:
		// Success
		model := effectiveModel(rec)
		if model == "" {
			return // nothing useful to log (e.g., count_tokens with no model)
		}
		parts := []string{colorize(model, colorCyan)}

		var tokens []string
		if rec.Usage.InputTokens > 0 {
			tokens = append(tokens, fmt.Sprintf("in:%s", formatTokenCount(rec.Usage.InputTokens)))
		}
		if rec.Usage.OutputTokens > 0 {
			tokens = append(tokens, fmt.Sprintf("out:%s", formatTokenCount(rec.Usage.OutputTokens)))
		}
		cacheTotal := rec.Usage.CacheReadInputTokens + rec.Usage.CacheCreationInputTokens
		if cacheTotal > 0 {
			tokens = append(tokens, fmt.Sprintf("cache:%s", formatTokenCount(cacheTotal)))
		}
		if len(tokens) > 0 {
			parts = append(parts, strings.Join(tokens, " "))
		}

		if winParts := formatUtilWindows(rec.Ratelimit.Windows); len(winParts) > 0 {
			parts = append(parts, strings.Join(winParts, " "))
		}

		fmt.Fprintf(a.logOutput, "[claude-meter] %s\n", strings.Join(parts, " | "))

	default:
		// Other errors (4xx, 5xx)
		model := effectiveModel(rec)
		msg := fmt.Sprintf("%d %s", rec.Status, http.StatusText(rec.Status))
		if model != "" {
			fmt.Fprintf(a.logOutput, "[claude-meter] %s | %s\n", colorize(model, colorYellow), colorize(msg, colorYellow))
		} else {
			fmt.Fprintf(a.logOutput, "[claude-meter] %s\n", colorize(msg, colorYellow))
		}
	}
}

func formatTokenCount(n int) string {
	if n >= 1_000_000 {
		return fmt.Sprintf("%.1fM", float64(n)/1_000_000)
	}
	if n >= 1_000 {
		return addCommas(n)
	}
	return fmt.Sprintf("%d", n)
}

func addCommas(n int) string {
	s := fmt.Sprintf("%d", n)
	if len(s) <= 3 {
		return s
	}
	var result []byte
	for i, c := range s {
		if i > 0 && (len(s)-i)%3 == 0 {
			result = append(result, ',')
		}
		result = append(result, byte(c))
	}
	return string(result)
}

func (a *App) trackAndMaybePrintStatus(rec normalize.Record) {
	a.mu.Lock()
	a.requestCount++
	count := a.requestCount

	model := effectiveModel(rec)
	if model != "" {
		a.modelCounts[model]++
	}
	for name, w := range rec.Ratelimit.Windows {
		a.lastUtil[name] = w.Utilization
	}

	shouldPrint := a.statusInterval > 0 && count%a.statusInterval == 0
	if !shouldPrint {
		a.mu.Unlock()
		return
	}

	// Snapshot data under lock, format outside
	modelSnapshot := make(map[string]uint64, len(a.modelCounts))
	for m, c := range a.modelCounts {
		modelSnapshot[m] = c
	}
	utilSnapshot := make(map[string]float64, len(a.lastUtil))
	for name, u := range a.lastUtil {
		utilSnapshot[name] = u
	}
	a.mu.Unlock()

	// Format summary outside the lock
	parts := []string{fmt.Sprintf("%d requests", count)}

	modelParts := []string{}
	for m, c := range modelSnapshot {
		short := m
		if idx := strings.LastIndex(m, "-"); idx > 0 {
			candidate := m[:idx]
			if strings.Contains(candidate, "opus") || strings.Contains(candidate, "sonnet") || strings.Contains(candidate, "haiku") {
				short = candidate
			}
		}
		modelParts = append(modelParts, fmt.Sprintf("%s:%d", short, c))
	}
	if len(modelParts) > 0 {
		parts = append(parts, strings.Join(modelParts, " "))
	}

	// Convert snapshot to RatelimitWindow map for formatUtilWindows
	snapWindows := make(map[string]normalize.RatelimitWindow, len(utilSnapshot))
	for name, u := range utilSnapshot {
		snapWindows[name] = normalize.RatelimitWindow{Utilization: u}
	}
	utilParts := formatUtilWindows(snapWindows)
	if len(utilParts) > 0 {
		parts = append(parts, strings.Join(utilParts, " "))
	}

	fmt.Fprintf(a.logOutput, "[claude-meter] %s\n", strings.Join(parts, " | "))
}

func (a *App) Handler() http.Handler {
	proxyHandler := a.proxy.Handler()
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Dashboard HTML (always available — embedded in binary)
		if r.Method == http.MethodGet && r.URL.Path == "/" && strings.Contains(r.Header.Get("Accept"), "text/html") {
			a.serveDashboard(w, r)
			return
		}
		// Stats API (must be before proxy to avoid capture)
		if r.Method == http.MethodGet && r.URL.Path == "/api/stats" {
			if a.analysisDir != "" {
				a.serveStats(w, r)
			} else {
				w.Header().Set("Content-Type", "application/json")
				w.Write([]byte(`{"error":"analysis not configured"}`))
			}
			return
		}
		proxyHandler.ServeHTTP(w, r)
	})
}

func (a *App) serveStats(w http.ResponseWriter, r *http.Request) {
	a.statsMu.Lock()
	if time.Since(a.statsCacheTime) < 5*time.Second && a.statsCache != nil {
		cached := a.statsCache
		a.statsMu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		w.Write(cached)
		return
	}
	a.statsMu.Unlock()

	script := filepath.Join(a.analysisDir, "dashboard.py")
	cmd := exec.CommandContext(r.Context(), "python3", script, a.logDir, "--api")
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		log.Printf("claude-meter: stats generation failed: %v\nstderr: %s", err, stderr.String())
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusInternalServerError)
		w.Write([]byte(`{"error":"failed to generate stats"}`))
		return
	}

	a.statsMu.Lock()
	a.statsCache = out
	a.statsCacheTime = time.Now()
	a.statsMu.Unlock()

	w.Header().Set("Content-Type", "application/json")
	w.Write(out)
}

func (a *App) serveDashboard(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Write(dashboardHTML)
}

func (a *App) Close() error {
	a.closeOnce.Do(func() {
		close(a.exchanges)
		a.wg.Wait()
	})
	return nil
}
