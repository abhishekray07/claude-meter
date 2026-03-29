package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"claude-meter-proxy/internal/app"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintf(os.Stderr, "usage: %s <start|setup|backfill-normalized> [options]\n", os.Args[0])
		os.Exit(2)
	}

	switch os.Args[1] {
	case "start":
		runStart(os.Args[2:])
	case "setup":
		runSetup(os.Args[2:])
	case "backfill-normalized":
		if err := runBackfillNormalized(os.Args[2:]); err != nil {
			log.Fatalf("backfill-normalized: %v", err)
		}
	default:
		fmt.Fprintf(os.Stderr, "usage: %s <start|setup|backfill-normalized> [options]\n", os.Args[0])
		os.Exit(2)
	}
}

func runStart(args []string) {
	startFlags := flag.NewFlagSet("start", flag.ExitOnError)
	port := startFlags.Int("port", 7735, "port to listen on")
	upstream := startFlags.String("upstream", "https://api.anthropic.com", "Anthropic upstream base URL")
	logDir := startFlags.String("log-dir", defaultLogDir(), "base log directory")
	queueSize := startFlags.Int("queue-size", 256, "in-memory completed exchange buffer")
	planTier := startFlags.String("plan-tier", "unknown", "declared plan tier for normalized records")
	analysisDir := startFlags.String("analysis-dir", "analysis", "path to the analysis/ directory containing dashboard.py")
	statusInterval := startFlags.Int("status-interval", 100, "print status summary every N requests (0 to disable)")
	startFlags.Parse(args)

	upstreamURL, err := url.Parse(*upstream)
	if err != nil {
		log.Fatalf("parse upstream URL: %v", err)
	}

	application, err := app.New(app.Config{
		UpstreamBaseURL: upstreamURL,
		LogDir:          expandHome(*logDir),
		QueueSize:       *queueSize,
		PlanTier:        *planTier,
		AnalysisDir:     *analysisDir,
		StatusInterval:  *statusInterval,
	})
	if err != nil {
		log.Fatalf("create app: %v", err)
	}
	defer application.Close()

	addr := fmt.Sprintf("127.0.0.1:%d", *port)
	server := &http.Server{
		Addr:    addr,
		Handler: application.Handler(),
	}

	go func() {
		fmt.Fprintf(os.Stderr, "\nclaude-meter proxy listening on http://%s\n", addr)
		fmt.Fprintf(os.Stderr, "forwarding to %s\n", upstreamURL.String())
		fmt.Fprintf(os.Stderr, "writing raw exchanges under %s\n", expandHome(*logDir))
		fmt.Fprintf(os.Stderr, "declared plan tier: %s\n\n", *planTier)
		fmt.Fprintf(os.Stderr, "Point Claude Code at it:\n")
		fmt.Fprintf(os.Stderr, "  ANTHROPIC_BASE_URL=http://%s claude\n\n", addr)
		fmt.Fprintf(os.Stderr, "To persist this, run: claude-meter setup\n\n")
		if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("listen: %v", err)
		}
	}()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	<-sigCh

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	_ = server.Shutdown(ctx)
}

func defaultLogDir() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return ".claude-meter"
	}
	return filepath.Join(home, ".claude-meter")
}

func expandHome(path string) string {
	if path == "~" {
		return defaultLogDir()
	}
	if strings.HasPrefix(path, "~/") {
		home, err := os.UserHomeDir()
		if err != nil {
			return path
		}
		return filepath.Join(home, path[2:])
	}
	return path
}
