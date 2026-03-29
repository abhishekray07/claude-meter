package main

import (
	"bufio"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

func runSetup(args []string) {
	setupFlags := flag.NewFlagSet("setup", flag.ExitOnError)
	port := setupFlags.Int("port", 7735, "port the proxy listens on")
	setupFlags.Parse(args)

	shellPath := os.Getenv("SHELL")
	shellName, rcSuffix, ok := detectShell(shellPath)
	if !ok {
		fmt.Fprintf(os.Stderr, "Could not detect shell (SHELL=%q).\n\n", shellPath)
		fmt.Fprintf(os.Stderr, "Add this to your shell config manually:\n")
		fmt.Fprintf(os.Stderr, "  export ANTHROPIC_BASE_URL=http://127.0.0.1:%d\n", *port)
		return
	}

	home, err := os.UserHomeDir()
	if err != nil {
		fmt.Fprintf(os.Stderr, "Could not determine home directory: %v\n", err)
		return
	}

	rcPath := filepath.Join(home, rcSuffix)
	line := exportLine(shellName, *port)

	// Warn if rc file is a symlink (could write to shared/system file)
	if info, err := os.Lstat(rcPath); err == nil && info.Mode()&os.ModeSymlink != 0 {
		target, _ := os.Readlink(rcPath)
		fmt.Fprintf(os.Stderr, "Warning: %s is a symlink to %s\n", rcPath, target)
		fmt.Fprintf(os.Stderr, "Add the line manually to be safe:\n  %s\n", line)
		return
	}

	if rcFileContainsExport(rcPath, shellName) {
		fmt.Fprintf(os.Stderr, "Already configured in %s\n", rcPath)
		return
	}

	fmt.Fprintf(os.Stderr, "Detected shell: %s\n", shellName)
	fmt.Fprintf(os.Stderr, "Add this to %s?\n\n", rcPath)
	fmt.Fprintf(os.Stderr, "  %s\n\n", line)
	fmt.Fprintf(os.Stderr, "Write to %s? [y/N] ", rcPath)

	reader := bufio.NewReader(os.Stdin)
	answer, _ := reader.ReadString('\n')
	answer = strings.TrimSpace(strings.ToLower(answer))

	if answer != "y" && answer != "yes" {
		fmt.Fprintf(os.Stderr, "\nSkipped. Add it manually:\n  %s\n", line)
		return
	}

	f, err := os.OpenFile(rcPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Could not open %s: %v\n", rcPath, err)
		fmt.Fprintf(os.Stderr, "Add it manually:\n  %s\n", line)
		return
	}
	defer f.Close()

	if _, err := fmt.Fprintf(f, "\n# claude-meter proxy\n%s\n", line); err != nil {
		fmt.Fprintf(os.Stderr, "Could not write to %s: %v\n", rcPath, err)
		return
	}

	fmt.Fprintf(os.Stderr, "Written to %s\n", rcPath)
	fmt.Fprintf(os.Stderr, "Restart your shell or run:\n  source %s\n", rcPath)
}

func detectShell(shellPath string) (name string, rcSuffix string, ok bool) {
	if shellPath == "" {
		return "", "", false
	}
	base := filepath.Base(shellPath)
	switch base {
	case "zsh":
		return "zsh", ".zshrc", true
	case "bash":
		return "bash", ".bashrc", true
	case "fish":
		return "fish", filepath.Join(".config", "fish", "config.fish"), true
	default:
		return "", "", false
	}
}

func exportLine(shellName string, port int) string {
	if shellName == "fish" {
		return fmt.Sprintf("set -gx ANTHROPIC_BASE_URL http://127.0.0.1:%d", port)
	}
	return fmt.Sprintf("export ANTHROPIC_BASE_URL=http://127.0.0.1:%d", port)
}

func rcFileContainsExport(path string, shellName string) bool {
	data, err := os.ReadFile(path)
	if err != nil {
		return false
	}
	content := string(data)
	if shellName == "fish" {
		return strings.Contains(content, "set -gx ANTHROPIC_BASE_URL") ||
			strings.Contains(content, "set -Ux ANTHROPIC_BASE_URL")
	}
	return strings.Contains(content, "export ANTHROPIC_BASE_URL=")
}
