// egress.go---aalegate egress allow-list proxy (plane 2).
//
// A tiny stdlib-only forward + CONNECT proxy: it allows requests ONLY to allow-listed
// hosts/domains and refuses (and logs) everything else. Supports both HTTPS CONNECT tunnels
// and plain HTTP forward-proxying (GET/POST/etc. with absolute URLs). It is the agent's single
// controlled route to the internet---the same static-binary model as the recorder: a container
// in docker mode, a host process in apptainer mode (bin/aalegate-egress). Standard library only;
// build CGO_ENABLED=0 -> scratch.
//
// Config (env):
//   EGRESS_LISTEN  bind address                          (default ":3128")
//   EGRESS_ALLOW   comma-separated allowed domains/IPs     (exact match, or any subdomain)
//   EGRESS_PORTS   comma-separated allowed ports           (default "443,80")
//   EGRESS_LOG     access-log path, appended               (default: stderr)   [plane 2]
//
// Log: "<ts> <client> ALLOW|DENY|FAIL <host:port>" per attempt; on tunnel close also
// "<ts> <client> CLOSE <host:port> sent=<bytes> recv=<bytes> dur=<ms>" (byte VOLUME, not content---
// flags large exfil / beaconing without breaking TLS).
package main

import (
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

func env(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func splitCSV(s string) []string {
	var out []string
	for _, p := range strings.Split(s, ",") {
		if p = strings.TrimSpace(strings.ToLower(p)); p != "" {
			out = append(out, p)
		}
	}
	return out
}

type proxy struct {
	domains []string
	ports   map[string]bool
	mu      sync.Mutex
	logw    io.Writer
}

// hostAllowed matches an exact domain or any subdomain of it (github.com allows api.github.com,
// but NOT evilgithub.com). Case-insensitive; trailing dot tolerated.
func (p *proxy) hostAllowed(host string) bool {
	host = strings.ToLower(strings.TrimSuffix(host, "."))
	for _, d := range p.domains {
		if host == d || strings.HasSuffix(host, "."+d) {
			return true
		}
	}
	return false
}

func (p *proxy) access(client, target, verdict string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	io.WriteString(p.logw, time.Now().UTC().Format(time.RFC3339)+" "+client+" "+verdict+" "+target+"\n")
}

// conn logs a tunnel's close with byte volumes (raw TLS bytes---volume, not content) + duration:
// sent = client->upstream (the agent's UPLOAD, the exfil-candidate direction), recv = upstream->client.
func (p *proxy) conn(client, target string, sent, recv int64, dur time.Duration) {
	p.mu.Lock()
	defer p.mu.Unlock()
	fmt.Fprintf(p.logw, "%s %s CLOSE %s sent=%d recv=%d dur=%dms\n",
		time.Now().UTC().Format(time.RFC3339), client, target, sent, recv, dur.Milliseconds())
}

func (p *proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	client, _, _ := net.SplitHostPort(r.RemoteAddr)
	if r.Method == http.MethodConnect {
		p.handleConnect(w, r, client)
	} else if r.URL.IsAbs() {
		p.handleHTTP(w, r, client)
	} else {
		p.access(client, r.Method+" "+r.RequestURI, "DENY-METHOD")
		http.Error(w, "egress: non-absolute request URI", http.StatusBadRequest)
	}
}

// handleConnect tunnels HTTPS through an allow-listed CONNECT proxy.
func (p *proxy) handleConnect(w http.ResponseWriter, r *http.Request, client string) {
	host, port, err := net.SplitHostPort(r.Host)
	if err != nil {
		http.Error(w, "egress: bad CONNECT target", http.StatusBadRequest)
		return
	}
	target := net.JoinHostPort(host, port)
	if !p.hostAllowed(host) || !p.ports[port] {
		p.access(client, target, "DENY")
		http.Error(w, "egress: destination not allow-listed", http.StatusForbidden)
		return
	}
	up, err := net.DialTimeout("tcp", target, 15*time.Second)
	if err != nil {
		p.access(client, target, "FAIL")
		http.Error(w, "egress: upstream dial failed", http.StatusBadGateway)
		return
	}
	p.access(client, target, "ALLOW")
	hj, ok := w.(http.Hijacker)
	if !ok {
		up.Close()
		http.Error(w, "egress: hijack unsupported", http.StatusInternalServerError)
		return
	}
	cc, _, err := hj.Hijack()
	if err != nil {
		up.Close()
		return
	}
	io.WriteString(cc, "HTTP/1.1 200 Connection Established\r\n\r\n")
	start := time.Now()
	sentCh := make(chan int64, 1)
	go func() { n, _ := io.Copy(up, cc); up.Close(); sentCh <- n }()
	recv, _ := io.Copy(cc, up)
	cc.Close()
	p.conn(client, target, <-sentCh, recv, time.Since(start))
}

// handleHTTP forward-proxies a plain HTTP request (absolute URL) to an allow-listed host.
func (p *proxy) handleHTTP(w http.ResponseWriter, r *http.Request, client string) {
	host := r.URL.Hostname()
	port := r.URL.Port()
	if port == "" {
		port = "80"
	}
	target := net.JoinHostPort(host, port)
	if !p.hostAllowed(host) || !p.ports[port] {
		p.access(client, r.Method+" "+target, "DENY")
		http.Error(w, "egress: destination not allow-listed", http.StatusForbidden)
		return
	}
	// Rewrite to a relative URL for the upstream server.
	r.RequestURI = ""
	r.Header.Del("Proxy-Connection")
	resp, err := (&http.Transport{}).RoundTrip(r)
	if err != nil {
		p.access(client, r.Method+" "+target, "FAIL")
		http.Error(w, "egress: upstream request failed", http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()
	p.access(client, r.Method+" "+target, "ALLOW")
	for k, vv := range resp.Header {
		for _, v := range vv {
			w.Header().Add(k, v)
		}
	}
	w.WriteHeader(resp.StatusCode)
	io.Copy(w, resp.Body)
}

func main() {
	log.SetFlags(log.LstdFlags | log.LUTC)
	p := &proxy{ports: map[string]bool{}, logw: os.Stderr}
	p.domains = splitCSV(env("EGRESS_ALLOW", ""))
	for _, pt := range splitCSV(env("EGRESS_PORTS", "443,80")) {
		p.ports[pt] = true
	}
	if lp := os.Getenv("EGRESS_LOG"); lp != "" {
		f, err := os.OpenFile(lp, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o640)
		if err != nil {
			log.Fatalf("open EGRESS_LOG %q: %v", lp, err)
		}
		defer f.Close()
		p.logw = f
	}
	listen := env("EGRESS_LISTEN", ":3128")
	log.Printf("aalegate-egress: listen %s allow=%v ports=%v", listen, p.domains, env("EGRESS_PORTS", "443,80"))
	log.Fatal((&http.Server{Addr: listen, Handler: p}).ListenAndServe())
}
