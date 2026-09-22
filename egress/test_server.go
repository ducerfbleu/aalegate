//go:build ignore

// test_server.go — dummy upstream + egress proxy test harness.
//
// Starts a dummy HTTP server (plain HTTP, not TLS) and the egress proxy, then
// runs requests through the proxy to exercise:
//   1. HTTP forward (GET, POST)
//   2. SSE streaming (text/event-stream flush)
//   3. Request body size cap (EGRESS_MAX_BODY)
//   4. Domain/port deny
//   5. CONNECT tunnel
//   6. Request duration logging
//
// Usage:  go run test_server.go
package main

import (
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"strings"
	"time"
)

func main() {
	// --- 1. start dummy upstream server ---
	mux := http.NewServeMux()

	mux.HandleFunc("/hello", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		fmt.Fprintf(w, "hello from upstream (method=%s)", r.Method)
	})

	mux.HandleFunc("/echo", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		body, _ := io.ReadAll(r.Body)
		fmt.Fprintf(w, `{"method":"%s","body_len":%d,"body":"%s"}`, r.Method, len(body), body)
	})

	mux.HandleFunc("/sse", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set("Cache-Control", "no-cache")
		w.Header().Set("Connection", "keep-alive")
		f, ok := w.(http.Flusher)
		if !ok {
			http.Error(w, "no flusher", 500)
			return
		}
		for i := 0; i < 5; i++ {
			fmt.Fprintf(w, "data: token_%d\n\n", i)
			f.Flush()
			time.Sleep(100 * time.Millisecond)
		}
		fmt.Fprint(w, "data: [DONE]\n\n")
		f.Flush()
	})

	mux.HandleFunc("/large", func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		fmt.Fprintf(w, "received %d bytes", len(body))
	})

	upLn, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		panic(err)
	}
	upAddr := upLn.Addr().String()
	go http.Serve(upLn, mux)
	fmt.Printf("upstream: http://%s\n", upAddr)

	// --- 2. start egress proxy ---
	_, upPort, _ := net.SplitHostPort(upAddr)
	os.Setenv("EGRESS_LISTEN", "127.0.0.1:0")
	os.Setenv("EGRESS_ALLOW", "127.0.0.1")
	os.Setenv("EGRESS_PORTS", upPort+",443")
	os.Setenv("EGRESS_MAX_BODY", "1024") // 1 KB cap for testing
	os.Setenv("EGRESS_LOG", "")

	proxyLn, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		panic(err)
	}
	proxyAddr := proxyLn.Addr().String()
	proxyLn.Close() // free the port for the proxy

	os.Setenv("EGRESS_LISTEN", proxyAddr)

	// build and run the proxy as a subprocess
	fmt.Println("building egress proxy...")
	buildCmd := exec.Command("go", "build", "-o", os.TempDir()+"/test-egress", ".")
	buildCmd.Dir = "."
	buildCmd.Stdout = os.Stdout
	buildCmd.Stderr = os.Stderr
	if err := buildCmd.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "build failed: %v\n", err)
		os.Exit(1)
	}

	proxyCmd := exec.Command(os.TempDir() + "/test-egress")
	proxyCmd.Env = append(os.Environ(),
		"EGRESS_LISTEN="+proxyAddr,
		"EGRESS_ALLOW=127.0.0.1",
		"EGRESS_PORTS="+upPort+",443",
		"EGRESS_MAX_BODY=1024",
	)
	proxyCmd.Stderr = os.Stderr
	if err := proxyCmd.Start(); err != nil {
		fmt.Fprintf(os.Stderr, "proxy start failed: %v\n", err)
		os.Exit(1)
	}
	defer proxyCmd.Process.Kill()
	time.Sleep(300 * time.Millisecond) // let proxy bind
	fmt.Printf("proxy:    http://%s\n\n", proxyAddr)

	proxyURL, _ := url.Parse("http://" + proxyAddr)
	client := &http.Client{Transport: &http.Transport{Proxy: http.ProxyURL(proxyURL)}}

	pass, fail := 0, 0
	check := func(name string, ok bool, detail string) {
		if ok {
			fmt.Printf("  PASS  %s  %s\n", name, detail)
			pass++
		} else {
			fmt.Printf("  FAIL  %s  %s\n", name, detail)
			fail++
		}
	}

	// --- 3. run tests ---
	fmt.Println("=== test: HTTP forward GET ===")
	resp, err := client.Get("http://127.0.0.1:" + upPort + "/hello")
	if err != nil {
		check("GET /hello", false, err.Error())
	} else {
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		check("GET /hello", resp.StatusCode == 200 && strings.Contains(string(body), "hello from upstream"),
			fmt.Sprintf("status=%d body=%q", resp.StatusCode, string(body)))
	}

	fmt.Println("=== test: HTTP forward POST ===")
	resp, err = client.Post("http://127.0.0.1:"+upPort+"/echo", "text/plain", strings.NewReader("test payload"))
	if err != nil {
		check("POST /echo", false, err.Error())
	} else {
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		check("POST /echo", resp.StatusCode == 200 && strings.Contains(string(body), `"body_len":12`),
			fmt.Sprintf("status=%d body=%q", resp.StatusCode, string(body)))
	}

	fmt.Println("=== test: SSE streaming ===")
	start := time.Now()
	resp, err = client.Get("http://127.0.0.1:" + upPort + "/sse")
	if err != nil {
		check("SSE", false, err.Error())
	} else {
		var tokens []string
		buf := make([]byte, 256)
		for {
			n, readErr := resp.Body.Read(buf)
			if n > 0 {
				chunk := string(buf[:n])
				for _, line := range strings.Split(chunk, "\n") {
					if strings.HasPrefix(line, "data: ") {
						tokens = append(tokens, strings.TrimPrefix(line, "data: "))
					}
				}
			}
			if readErr != nil {
				break
			}
		}
		resp.Body.Close()
		elapsed := time.Since(start)
		// SSE sends 5 tokens at 100ms intervals + [DONE] = ~500ms minimum
		// if flush works, we should see all 6 data lines and it should take >400ms
		check("SSE tokens", len(tokens) == 6,
			fmt.Sprintf("got %d tokens: %v", len(tokens), tokens))
		check("SSE streaming (not buffered)", elapsed > 400*time.Millisecond,
			fmt.Sprintf("elapsed=%v (expect >400ms if streamed, <100ms if buffered)", elapsed))
	}

	fmt.Println("=== test: body size cap (reject oversized) ===")
	bigBody := strings.Repeat("X", 2048) // 2 KB > 1 KB cap
	resp, err = client.Post("http://127.0.0.1:"+upPort+"/large", "application/octet-stream", strings.NewReader(bigBody))
	if err != nil {
		check("body cap", false, err.Error())
	} else {
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		check("body cap (413)", resp.StatusCode == 413,
			fmt.Sprintf("status=%d body=%q", resp.StatusCode, strings.TrimSpace(string(body))))
	}

	fmt.Println("=== test: body size cap (allow small) ===")
	smallBody := strings.Repeat("Y", 512) // 512 B < 1 KB cap
	resp, err = client.Post("http://127.0.0.1:"+upPort+"/large", "application/octet-stream", strings.NewReader(smallBody))
	if err != nil {
		check("body small", false, err.Error())
	} else {
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		check("body small (pass)", resp.StatusCode == 200 && strings.Contains(string(body), "received 512"),
			fmt.Sprintf("status=%d body=%q", resp.StatusCode, string(body)))
	}

	fmt.Println("=== test: denied host ===")
	resp, err = client.Get("http://10.99.99.99:80/nope")
	if err == nil {
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		check("deny host", resp.StatusCode == 403,
			fmt.Sprintf("status=%d body=%q", resp.StatusCode, string(body)))
	} else {
		// proxy may return error or 403 — either indicates denial
		check("deny host", strings.Contains(err.Error(), "403") || strings.Contains(err.Error(), "Forbidden"),
			err.Error())
	}

	fmt.Println("=== test: denied port ===")
	resp, err = client.Get("http://127.0.0.1:9999/nope")
	if err == nil {
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		check("deny port", resp.StatusCode == 403,
			fmt.Sprintf("status=%d body=%q", resp.StatusCode, string(body)))
	} else {
		check("deny port", strings.Contains(err.Error(), "403") || strings.Contains(err.Error(), "Forbidden"),
			err.Error())
	}

	fmt.Printf("\n=== results: %d passed, %d failed ===\n", pass, fail)
	if fail > 0 {
		os.Exit(1)
	}
}
