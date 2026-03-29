package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestDetectShell(t *testing.T) {
	tests := []struct {
		shell    string
		wantName string
		wantRC   string
		wantOk   bool
	}{
		{"/bin/zsh", "zsh", ".zshrc", true},
		{"/bin/bash", "bash", ".bashrc", true},
		{"/usr/bin/fish", "fish", ".config/fish/config.fish", true},
		{"/usr/local/bin/fish", "fish", ".config/fish/config.fish", true},
		{"/bin/sh", "", "", false},
		{"", "", "", false},
	}

	for _, tt := range tests {
		t.Run(tt.shell, func(t *testing.T) {
			name, rcSuffix, ok := detectShell(tt.shell)
			if ok != tt.wantOk {
				t.Errorf("detectShell(%q) ok = %v, want %v", tt.shell, ok, tt.wantOk)
			}
			if name != tt.wantName {
				t.Errorf("detectShell(%q) name = %q, want %q", tt.shell, name, tt.wantName)
			}
			if rcSuffix != tt.wantRC {
				t.Errorf("detectShell(%q) rc = %q, want %q", tt.shell, rcSuffix, tt.wantRC)
			}
		})
	}
}

func TestExportLine(t *testing.T) {
	tests := []struct {
		shell string
		port  int
		want  string
	}{
		{"zsh", 7735, "export ANTHROPIC_BASE_URL=http://127.0.0.1:7735"},
		{"bash", 7735, "export ANTHROPIC_BASE_URL=http://127.0.0.1:7735"},
		{"fish", 7735, "set -gx ANTHROPIC_BASE_URL http://127.0.0.1:7735"},
	}

	for _, tt := range tests {
		t.Run(tt.shell, func(t *testing.T) {
			got := exportLine(tt.shell, tt.port)
			if got != tt.want {
				t.Errorf("exportLine(%q, %d) = %q, want %q", tt.shell, tt.port, got, tt.want)
			}
		})
	}
}

func TestRCFileAlreadyConfigured(t *testing.T) {
	dir := t.TempDir()
	rcFile := filepath.Join(dir, ".zshrc")
	os.WriteFile(rcFile, []byte("# my config\nexport ANTHROPIC_BASE_URL=http://127.0.0.1:7735\n"), 0644)

	if !rcFileContainsExport(rcFile, "zsh") {
		t.Error("expected rcFileContainsExport to return true")
	}
}

func TestRCFileNotConfigured(t *testing.T) {
	dir := t.TempDir()
	rcFile := filepath.Join(dir, ".zshrc")
	os.WriteFile(rcFile, []byte("# my config\nexport PATH=/usr/bin\n"), 0644)

	if rcFileContainsExport(rcFile, "zsh") {
		t.Error("expected rcFileContainsExport to return false")
	}
}

func TestRCFileCommentNotMatched(t *testing.T) {
	dir := t.TempDir()
	rcFile := filepath.Join(dir, ".zshrc")
	os.WriteFile(rcFile, []byte("# OLD: ANTHROPIC_BASE_URL was removed\n"), 0644)

	if rcFileContainsExport(rcFile, "zsh") {
		t.Error("expected comment-only mention to return false")
	}
}

func TestRCFileFishSyntax(t *testing.T) {
	dir := t.TempDir()
	rcFile := filepath.Join(dir, "config.fish")
	os.WriteFile(rcFile, []byte("set -gx ANTHROPIC_BASE_URL http://127.0.0.1:7735\n"), 0644)

	if !rcFileContainsExport(rcFile, "fish") {
		t.Error("expected fish export to return true")
	}
}

func TestRCFileMissing(t *testing.T) {
	if rcFileContainsExport("/nonexistent/.zshrc", "zsh") {
		t.Error("expected rcFileContainsExport to return false for missing file")
	}
}
