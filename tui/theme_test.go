package main

import "testing"

func TestPalettes(t *testing.T) {
	// init() defaults to nocturnal (256-color accent 75).
	setPalette("nocturnal")
	if cAccent != "38;5;75" {
		t.Fatalf("nocturnal accent = %q, want 38;5;75", cAccent)
	}
	// switching applies immediately to the active color vars.
	if !setPalette("dracula") {
		t.Fatal("setPalette(dracula) returned false")
	}
	if cAccent != "38;5;141" {
		t.Fatalf("dracula accent = %q, want 38;5;141", cAccent)
	}
	// unknown theme reports false and leaves the palette unchanged.
	if setPalette("nope") {
		t.Fatal("setPalette(nope) should be false")
	}
	if cAccent != "38;5;141" {
		t.Fatalf("unknown theme mutated palette: %q", cAccent)
	}
	// every listed theme resolves and defines all seven roles.
	if len(themeNames()) != 6 {
		t.Fatalf("expected 6 themes, got %d", len(themeNames()))
	}
	for _, n := range themeNames() {
		if !setPalette(n) {
			t.Fatalf("theme %q not resolvable", n)
		}
		for role, code := range map[string]string{"green": cGreen, "yellow": cYellow, "red": cRed, "dim": cDim, "accent": cAccent, "white": cWhite, "border": cBorder} {
			if code == "" {
				t.Fatalf("theme %q missing %s", n, role)
			}
		}
	}
	setPalette("nocturnal") // restore default for other tests
}
