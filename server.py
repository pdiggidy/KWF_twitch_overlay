#!/usr/bin/env python3
"""
KWF Charity Stream Overlay Server
Scrapes acties.kwf.nl every 60s and serves the OBS browser source overlay.

Usage:
    pip install requests beautifulsoup4
    python server.py

Then in OBS: add Browser Source → http://localhost:8080/overlay
Other streamers on your network: http://YOUR_LOCAL_IP:8080/overlay
"""

import csv
import json
import queue
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

import requests
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────────────────────────────
KWF_URL    = "TEMP"
GOAL       = 1000        # € goal — update if you change it on KWF
PORT       = 8080
POLL_SEC   = 30        # how often to re-scrape (seconds)
LOG_FILE   = Path(__file__).parent / "donations.csv"
debug = False
# ─────────────────────────────────────────────────────────────────────────────

state = {
    "raised":    0.0,
    "goal":      GOAL,
    "last_updated": "never",
    "donors": [],         # list of {"name": str, "amount": float} — last 5
}
state_lock = threading.Lock()

# ── SSE client registry ───────────────────────────────────────────────────────
sse_clients: list[queue.Queue] = []
sse_lock = threading.Lock()

def broadcast():
    """Push current state to every connected SSE client."""
    with state_lock:
        msg = json.dumps(state)
    with sse_lock:
        for q in sse_clients[:]:
            q.put(msg)

# ── CSV logging ───────────────────────────────────────────────────────────────

def init_csv():
    """Create the CSV with a header row if it doesn't exist yet."""
    if not LOG_FILE.exists():
        with open(LOG_FILE, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["timestamp", "raised", "goal"])
        print(f"[log] Created {LOG_FILE}")

def log_row(raised: float, goal: float):
    """Append one row to the CSV. Only called when the value changes."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([ts, f"{raised:.2f}", f"{goal:.2f}"])
    print(f"[log] Wrote €{raised:.2f} @ {ts}")

# ─────────────────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

def parse_euro(text: str) -> float:
    """Turn '€ 1.234,56' or '€1000' into a float."""
    clean = re.sub(r"[€\s]", "", text)
    # Dutch format: dots as thousands sep, comma as decimal
    if "," in clean:
        clean = clean.replace(".", "").replace(",", ".")
    else:
        clean = clean.replace(".", "")
    try:
        return float(clean)
    except ValueError:
        return 0.0

def scrape():
    """Fetch KWF page and extract raised amount + recent donors."""
    global debug
    try:
        resp = requests.get(KWF_URL, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"[scraper] fetch error: {e}")
        return

    try:
        soup = BeautifulSoup(resp.text, "html.parser")

        raised = 0.0
        goal   = GOAL

        # ── Amount raised ──────────────────────────────────────────────────────
        # The page has a structure like:
        #   <h3>€2</h3> after the label "Opgehaald"
        # Strategy: find all h3 tags containing a € sign
        tags = soup.find(class_="statistics").find_all("h3", class_="money")
        tag = tags[0]
        txt = tag.get_text(strip=True)
        if "€" in txt and re.search(r"\d", txt):
            val = parse_euro(txt)
            raised = val

        goal = tags[1].get_text(strip=True)
        if "€" in goal and re.search(r"\d", goal):
            goal = parse_euro(goal)

        # ── Recent donors ─────────────────────────────────────────────────────
        donors = []
        # Donor names often appear in a list section
        donor_section = soup.find(string=re.compile("donateurs", re.I))
        if donor_section:
            parent = donor_section.find_parent()
            if parent:
                for item in parent.find_next_siblings()[:10]:
                    name_tag = item.find(["strong", "span", "p", "h4", "h5"])
                    if name_tag:
                        name = name_tag.get_text(strip=True)
                        if name and len(name) > 1:
                            donors.append({"name": name, "amount": None})
                    if len(donors) >= 5:
                        break

    except Exception as e:
        print(f"[scraper] parse error: {e}")
        return

    with state_lock:
        prev_raised = state["raised"]
        state["raised"]       = raised
        if debug:
            state["raised"] = prev_raised + 1
            print(f"[debug] Simulated raised: €{state['raised']:.2f}")
        state["goal"]         = goal
        state["last_updated"] = time.strftime("%H:%M:%S")
        state["donors"]       = donors

    pct = (raised / goal * 100) if goal else 0
    print(f"[scraper] €{raised:.0f} / €{goal:.0f}  ({pct:.1f}%)  @ {state['last_updated']}")

    # Log to CSV and push to browsers whenever the raised amount changes
    with state_lock:
        effective_raised = state["raised"]
    if effective_raised != prev_raised:
        log_row(effective_raised, goal)
        broadcast()


def scraper_loop():
    while True:
        scrape()
        time.sleep(POLL_SEC)


# ── Read the overlay HTML once from disk ──────────────────────────────────────
OVERLAY_HTML = (Path(__file__).parent / "overlay.html").read_text(encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence access log

    def send(self, code, ctype, body):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(b))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(b)
        except (ConnectionAbortedError, BrokenPipeError):
            pass  # client disconnected before response completed

    def handle_error(self, request, client_address):
        pass  # suppress connection reset tracebacks in the console

    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/overlay", "/overlay/", "/"):
            self.send(200, "text/html; charset=utf-8", OVERLAY_HTML)

        elif path == "/events":
            # Server-Sent Events — keep connection open and push updates
            q: queue.Queue = queue.Queue()
            with sse_lock:
                sse_clients.append(q)

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")  # disable nginx buffering if tunnelled
            self.send_header("Transfer-Encoding", "chunked")  # prevent Cloudflare response buffering
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            # Send current state immediately so the overlay populates on connect
            with state_lock:
                initial = json.dumps(state)
            try:
                self.wfile.write(f"data: {initial}\n\n".encode())
                self.wfile.flush()
            except (ConnectionAbortedError, BrokenPipeError, OSError):
                with sse_lock:
                    try: sse_clients.remove(q)
                    except ValueError: pass
                return

            try:
                while True:
                    try:
                        msg = q.get(timeout=25)
                        self.wfile.write(f"data: {msg}\n\n".encode())
                        self.wfile.flush()
                    except queue.Empty:
                        # Keepalive comment — prevents proxies/tunnels from closing idle connections
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except (ConnectionAbortedError, BrokenPipeError, OSError):
                pass
            finally:
                with sse_lock:
                    try: sse_clients.remove(q)
                    except ValueError: pass

        elif path == "/KWF_qr.png":
            img_path = Path(__file__).parent / "KWF_qr.png"
            if img_path.exists():
                data = img_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", len(data))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send(404, "text/plain", "not found")

        else:
            self.send(404, "text/plain", "not found")


if __name__ == "__main__":
    # Set up CSV log file
    init_csv()

    # Initial scrape before server starts
    print("[init] Doing initial scrape...")
    scrape()

    # Start background scraper
    t = threading.Thread(target=scraper_loop, daemon=True)
    t.start()


    # Start HTTP server (threaded so multiple SSE connections don't block each other)
    class ThreadingServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

    server = ThreadingServer(("0.0.0.0", PORT), Handler)
    print(f"\n✅  Overlay server running!")
    print(f"   Local:   http://localhost:{PORT}/overlay")
    print(f"   Network: http://<your-ip>:{PORT}/overlay")
    print(f"\n   Scraping every {POLL_SEC}s — press Ctrl+C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
