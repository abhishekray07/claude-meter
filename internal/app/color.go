package app

import "os"

const (
	colorReset  = "\033[0m"
	colorRed    = "\033[31m"
	colorGreen  = "\033[32m"
	colorYellow = "\033[33m"
	colorCyan   = "\033[36m"
)

// colorize wraps text in ANSI color codes, respecting NO_COLOR env var.
func colorize(text, color string) string {
	return colorizeWith(text, color, os.Getenv("NO_COLOR") != "")
}

// colorizeWith is the testable version that accepts noColor as a parameter
// instead of reading the environment (avoids race conditions in parallel tests).
func colorizeWith(text, color string, noColor bool) string {
	if noColor {
		return text
	}
	return color + text + colorReset
}

func utilizationColor(utilization float64) string {
	if utilization >= 0.80 {
		return colorRed
	}
	if utilization >= 0.50 {
		return colorYellow
	}
	return colorGreen
}
