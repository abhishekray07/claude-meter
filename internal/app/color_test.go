package app

import (
	"testing"
)

func TestColorize(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name    string
		input   string
		color   string
		want    string
		noColor bool
	}{
		{"cyan text", "hello", colorCyan, "\033[36mhello\033[0m", false},
		{"red text", "err", colorRed, "\033[31merr\033[0m", false},
		{"green text", "ok", colorGreen, "\033[32mok\033[0m", false},
		{"yellow text", "warn", colorYellow, "\033[33mwarn\033[0m", false},
		{"no color strips ansi", "hello", colorCyan, "hello", true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			got := colorizeWith(tt.input, tt.color, tt.noColor)
			if got != tt.want {
				t.Errorf("colorizeWith(%q, %q, %v) = %q, want %q", tt.input, tt.color, tt.noColor, got, tt.want)
			}
		})
	}
}

func TestUtilizationColor(t *testing.T) {
	t.Parallel()

	tests := []struct {
		utilization float64
		want        string
	}{
		{0.10, colorGreen},
		{0.49, colorGreen},
		{0.50, colorYellow},
		{0.79, colorYellow},
		{0.80, colorRed},
		{1.00, colorRed},
	}

	for _, tt := range tests {
		got := utilizationColor(tt.utilization)
		if got != tt.want {
			t.Errorf("utilizationColor(%v) = %q, want %q", tt.utilization, got, tt.want)
		}
	}
}
