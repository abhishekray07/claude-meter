package app

import (
	"bytes"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"

	"claude-meter-proxy/internal/capture"
	"claude-meter-proxy/internal/normalize"
	"claude-meter-proxy/internal/proxy"
	"claude-meter-proxy/internal/storage"
)

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
}

type App struct {
	proxy            *proxy.Server
	exchanges        chan capture.CompletedExchange
	rawWriter        rawWriter
	normalizedWriter normalizedWriter
	normalizer       normalizer
	logDir           string
	analysisDir      string

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
	app := &App{
		exchanges:        exchanges,
		rawWriter:        rw,
		normalizedWriter: nw,
		normalizer:       norm,
		logDir:           cfg.LogDir,
		analysisDir:      cfg.AnalysisDir,
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
	if err := a.normalizedWriter.Write(a.normalizer.Normalize(ex)); err != nil {
		log.Printf("claude-meter: normalized write error for exchange %d: %v", ex.ID, err)
	}
}

func (a *App) Handler() http.Handler {
	proxyHandler := a.proxy.Handler()
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if a.analysisDir != "" && r.Method == http.MethodGet && r.URL.Path == "/" && strings.Contains(r.Header.Get("Accept"), "text/html") {
			a.serveDashboard(w, r)
			return
		}
		proxyHandler.ServeHTTP(w, r)
	})
}

func (a *App) serveDashboard(w http.ResponseWriter, r *http.Request) {
	script := filepath.Join(a.analysisDir, "dashboard.py")
	cmd := exec.CommandContext(r.Context(), "python3", script, a.logDir, "--output", "-")
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		log.Printf("claude-meter: dashboard generation failed: %v\nstderr: %s", err, stderr.String())
		http.Error(w, "failed to generate dashboard", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Write(out)
}

func (a *App) Close() error {
	a.closeOnce.Do(func() {
		close(a.exchanges)
		a.wg.Wait()
	})
	return nil
}
