package app

import (
	"fmt"
	"log"
	"net/http"
	"net/url"
	"path/filepath"
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
}

type App struct {
	proxy            *proxy.Server
	exchanges        chan capture.CompletedExchange
	rawWriter        rawWriter
	normalizedWriter normalizedWriter
	normalizer       normalizer

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

	exchanges := make(chan capture.CompletedExchange, cfg.QueueSize)
	app := &App{
		exchanges:        exchanges,
		rawWriter:        rw,
		normalizedWriter: nw,
		normalizer:       norm,
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
	return a.proxy.Handler()
}

func (a *App) Close() error {
	a.closeOnce.Do(func() {
		close(a.exchanges)
		a.wg.Wait()
	})
	return nil
}
