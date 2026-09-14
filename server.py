#!/usr/bin/env python3
"""
AI Viral Product Finder - backend.

Serves the app and talks to Google Gemini on the visitor's behalf, so the API
key lives only on this server and never reaches anyone's browser.

Standard library only: nothing to pip install.

Run locally:
    python3 server.py            (or double-click start-server.command)
    then open http://localhost:8000

The key:
    Locally, put   GEMINI_API_KEY=your-key   in the .env file next to this script.
    On a host (Render, Railway...), set GEMINI_API_KEY as an environment variable
    in their dashboard instead - never upload the .env file.

Optional environment variables:
    PORT             port to listen on (hosts set this for you). Default 8000.
    RATE_PER_MINUTE  scans one visitor may run per minute.       Default 6.
    RATE_PER_DAY     scans one visitor may run per day.          Default 60.
    GLOBAL_PER_DAY   scans the whole site may run per day.       Default 1000.
                     Kept under Gemini's free daily quota so strangers
                     cannot exhaust your key.
"""

import base64
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
APP_FILE = os.path.join(HERE, "viral-product-finder-STANDALONE.html")
ENV_FILE = os.path.join(HERE, ".env")

GEMINI_API_BASE = os.environ.get("GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta")
# A 6MB photo is ~8MB once base64-encoded, so the body limit sits a little
# above that: photos near the limit get a readable "too large" answer, and
# only genuinely absurd uploads are refused before being read.
MAX_IMAGE_BYTES = 6 * 1024 * 1024
MAX_BODY_BYTES = 9 * 1024 * 1024
RATE_PER_MINUTE = int(os.environ.get("RATE_PER_MINUTE", "6"))
RATE_PER_DAY = int(os.environ.get("RATE_PER_DAY", "60"))
GLOBAL_PER_DAY = int(os.environ.get("GLOBAL_PER_DAY", "1000"))
RETRY_PAUSE_SECONDS = float(os.environ.get("RETRY_PAUSE_SECONDS", "1.5"))

# Only these paths are ever served. Everything else in the folder - above all
# .env and this file - returns 404, so the key cannot be downloaded.
APP_PATHS = {"/", "/index.html", "/viral-product-finder-STANDALONE.html"}


class ClientError(Exception):
    """Something wrong with the visitor's request. Safe to show them."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def read_env_file():
    values = {}
    try:
        with open(ENV_FILE, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                values[name.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return values


def api_key():
    """Read on every request, so adding the key to .env needs no restart."""
    return (os.environ.get("GEMINI_API_KEY") or read_env_file().get("GEMINI_API_KEY", "")).strip()


def load_prompt():
    """
    The identification prompt is defined once, in the app's HTML, and read
    from there - so the in-browser mode and this server can never drift apart.
    The server supplies the prompt itself: visitors only send an image, which
    stops anyone using your key as a free general-purpose chatbot.
    """
    with open(APP_FILE, encoding="utf-8") as handle:
        html = handle.read()
    match = re.search(r"const IDENTIFY_PROMPT = `(.*?)`;", html, re.S)
    if not match or "${" in match.group(1):
        raise RuntimeError("Could not find IDENTIFY_PROMPT in " + os.path.basename(APP_FILE))
    # Undo the escaping a JavaScript template literal applies (\" -> ").
    return re.sub(r"\\(.)", r"\1", match.group(1))


# --------------------------------------------------------------------------
# Abuse protection
# --------------------------------------------------------------------------

class RateLimiter:
    """In-memory limits. Reset when the server restarts, which is fine for this."""

    def __init__(self):
        self.lock = threading.Lock()
        self.by_ip = {}
        self.everyone = []

    def check(self, ip):
        now = time.time()
        day_ago = now - 86400
        with self.lock:
            self.everyone = [t for t in self.everyone if t > day_ago]
            hits = [t for t in self.by_ip.get(ip, []) if t > day_ago]

            if len(self.everyone) >= GLOBAL_PER_DAY:
                return "This site has reached its scan limit for today. Please try again tomorrow."
            if len(hits) >= RATE_PER_DAY:
                return "You've reached today's scan limit. Please try again tomorrow."
            if len([t for t in hits if t > now - 60]) >= RATE_PER_MINUTE:
                return "That's a lot of scans in a row. Wait a minute and try again."

            hits.append(now)
            self.everyone.append(now)
            self.by_ip[ip] = hits
            for stale_ip in [k for k, v in self.by_ip.items() if not v or v[-1] <= day_ago]:
                del self.by_ip[stale_ip]
            return None


LIMITER = RateLimiter()


# --------------------------------------------------------------------------
# Google Gemini
# --------------------------------------------------------------------------

def google(path, key, payload=None):
    """
    One call to Gemini, returning (status, json_body).

    Google is migrating from "AIza" keys to "AQ." auth keys, and the two are not
    accepted identically everywhere: send the documented header first, and if
    that is a 401, retry as a bearer token.
    """
    url = GEMINI_API_BASE + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None

    def attempt(auth):
        headers = {"Content-Type": "application/json"}
        headers.update(auth)
        request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read() or b"{}")
            except ValueError:
                return error.code, {}
        except (urllib.error.URLError, OSError):
            raise ClientError(502, "The server could not reach Google. Please try again in a moment.")

    status, body = attempt({"x-goog-api-key": key})
    if status == 401:
        status, body = attempt({"Authorization": "Bearer " + key})
    return status, body


def google_error(status, body):
    detail = ((body or {}).get("error") or {}).get("message", "")
    log("Gemini error %s: %s" % (status, detail or body))

    if status in (400, 401, 403) and (status != 400 or re.search("API key", detail, re.I)):
        # The visitor can't fix this - it's the site owner's key.
        return ClientError(502, "The server's Gemini API key was rejected by Google. "
                                "The site owner needs to check GEMINI_API_KEY.")
    if status == 429:
        return ClientError(503, "The free AI quota is used up for now. Please try again in a few minutes.")
    if status >= 500:
        return ClientError(503, "Google's AI is overloaded right now (%s). Please try again in a minute." % status)
    return ClientError(502, "Google returned an error (status %s)%s" % (status, ": " + detail if detail else "."))


def score_model(name):
    """Rank a model name for this job: a current, general-purpose Flash model."""
    if re.search(r"embed|aqa|imagen|veo|tts|audio|live|image-generation|vision-latest", name, re.I):
        return -1
    score = 0.0
    if re.search("flash", name, re.I):
        score += 100
    elif re.search("pro", name, re.I):
        score += 40
    if re.search("lite", name, re.I):
        score -= 25
    if re.search("latest", name, re.I):
        score += 30
    if re.search(r"preview|exp\b", name, re.I):
        score -= 15
    version = re.search(r"(\d+(?:\.\d+)?)", name)
    if version:
        score += float(version.group(1)) * 2
    return score


class ModelList:
    """Which models this key can use, best first. Cached until a scan finds it stale."""

    def __init__(self):
        self.lock = threading.Lock()
        self.key = None
        self.ranked = []

    def get(self, key, refresh=False):
        with self.lock:
            if self.ranked and self.key == key and not refresh:
                return list(self.ranked)

        status, body = google("/models", key)
        if status != 200:
            raise google_error(status, body)

        ranked = []
        for model in body.get("models", []):
            if "generateContent" not in model.get("supportedGenerationMethods", []):
                continue
            name = str(model.get("name", "")).replace("models/", "", 1)
            if score_model(name) > 0:
                ranked.append(name)
        ranked.sort(key=score_model, reverse=True)
        if not ranked:
            raise ClientError(502, "The server's Gemini key has no usable model available.")

        log("Gemini models, best first: %s" % ", ".join(ranked[:4]))
        with self.lock:
            self.key, self.ranked = key, ranked
        return list(ranked)

    def promote(self, name):
        with self.lock:
            if name in self.ranked:
                self.ranked.remove(name)
                self.ranked.insert(0, name)


MODELS = ModelList()


def parse_answer(text):
    """Models sometimes wrap JSON in fences or prose - dig it out."""
    text = re.sub(r"^```(?:json)?", "", str(text or "").strip(), flags=re.I)
    text = re.sub(r"```$", "", text).strip()
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    return None


def identify(image_data_url, key, refreshed=False):
    match = re.match(r"^data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=]+)$", image_data_url or "")
    if not match:
        raise ClientError(400, "Please send a JPG, PNG or WEBP photo.")
    mime, data = match.group(1), match.group(2)
    try:
        size = len(base64.b64decode(data, validate=True))
    except ValueError:
        raise ClientError(400, "That photo could not be read. Please try another one.")
    if size > MAX_IMAGE_BYTES:
        raise ClientError(413, "That photo is too large. Please use one under 6MB.")

    prompt = load_prompt()
    models = MODELS.get(key, refresh=refreshed)
    shortlist = models[:4]

    # The free tier routinely answers 503 "high demand" and 429 on popular
    # models, and a 500 can mean JSON mode was disliked. Each model gets three
    # shots - once, once more after a pause, once without JSON mode - before we
    # move down to a less contended model.
    attempts = [(True, 0), (True, RETRY_PAUSE_SECONDS), (False, 0)]
    last_error = ClientError(503, "Google's AI could not be reached. Please try again in a minute.")
    retired = 0

    for model in shortlist:
        for json_mode, pause in attempts:
            if pause:
                time.sleep(pause)
            config = {"maxOutputTokens": 4096, "temperature": 0.2}
            if json_mode:
                config["responseMimeType"] = "application/json"
            status, body = google("/models/%s:generateContent" % model, key, {
                "contents": [{"parts": [
                    {"text": prompt},
                    {"inlineData": {"mimeType": mime, "data": data}},
                ]}],
                "generationConfig": config,
            })

            if status == 404:
                retired += 1
                break
            if status != 200:
                last_error = google_error(status, body)
                if last_error.status == 502 and "rejected" in last_error.message:
                    raise last_error  # a bad key will not improve with retries
                continue

            candidates = body.get("candidates") or []
            if not candidates:
                blocked = (body.get("promptFeedback") or {}).get("blockReason")
                if blocked:
                    raise ClientError(422, "Google declined to analyze that image (%s). Try a different photo." % blocked)
                continue
            candidate = candidates[0]
            if candidate.get("finishReason") in ("SAFETY", "PROHIBITED_CONTENT"):
                raise ClientError(422, "Google blocked that image on safety grounds. Try a different photo.")

            parts = (candidate.get("content") or {}).get("parts") or []
            answer = parse_answer("".join(part.get("text", "") for part in parts))
            if answer is not None:
                MODELS.promote(model)
                return answer
            last_error = ClientError(502, "The AI's answer could not be read. Please try a clearer photo.")

    if retired == len(shortlist) and not refreshed:
        log("All cached Gemini models are gone - refreshing the list")
        return identify(image_data_url, key, refreshed=True)
    raise last_error


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def log(message):
    sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), message))
    sys.stderr.flush()


class Handler(BaseHTTPRequestHandler):
    server_version = "ProductFinder/1.0"

    def log_message(self, fmt, *args):
        log("%s %s" % (self.client_ip(), fmt % args))

    def client_ip(self):
        # Hosts like Render put the real visitor address in X-Forwarded-For.
        forwarded = self.headers.get("X-Forwarded-For", "")
        return forwarded.split(",")[0].strip() or self.client_address[0]

    def send(self, status, body, content_type):
        payload = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, status, data):
        self.send(status, json.dumps(data), "application/json; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in APP_PATHS:
            with open(APP_FILE, "rb") as handle:
                self.send(200, handle.read(), "text/html; charset=utf-8")
        elif path == "/api/health":
            self.send_json(200, {"ok": True, "keyConfigured": bool(api_key())})
        else:
            self.send(404, "Not found", "text/plain; charset=utf-8")

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/api/identify":
            self.send_json(404, {"error": "Not found"})
            return
        try:
            self.send_json(200, self.handle_identify())
        except ClientError as error:
            self.send_json(error.status, {"error": error.message})
        except Exception as error:  # never leak a traceback to a visitor
            log("Unexpected error: %r" % error)
            self.send_json(500, {"error": "Something went wrong on the server. Please try again."})

    def handle_identify(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            raise ClientError(400, "No photo was sent.")
        if length > MAX_BODY_BYTES:
            raise ClientError(413, "That photo is too large. Please use one under 6MB.")

        key = api_key()
        if not key:
            where = "in the host's Environment settings" if "PORT" in os.environ else "to the .env file"
            raise ClientError(503, "The server has no Gemini API key yet. The site owner needs to add "
                                   "GEMINI_API_KEY %s." % where)

        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            raise ClientError(400, "The request could not be read.")
        image = body.get("image") if isinstance(body, dict) else None

        limited = LIMITER.check(self.client_ip())
        if limited:
            raise ClientError(429, limited)

        return identify(image, key)


def main():
    # Hosts capture output through a pipe, where Python would otherwise hold
    # the startup messages back - including the "key MISSING" warning.
    sys.stdout.reconfigure(line_buffering=True)
    load_prompt()  # fail fast if the app file moved or the prompt can't be found

    on_a_host = "PORT" in os.environ
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST") or ("0.0.0.0" if on_a_host else "127.0.0.1")

    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError:
        print("\n  Port %d is already in use - the server is probably already running." % port)
        print("  Open http://localhost:%d in your browser.\n" % port)
        sys.exit(1)

    url = "http://localhost:%d" % port
    if on_a_host:
        print("\n  AI Viral Product Finder is running on port %d" % port)
    else:
        print("\n  AI Viral Product Finder is running at %s" % url)
    if api_key():
        print("  Gemini API key: found (kept on the server, never sent to browsers)")
    elif on_a_host:
        print("  Gemini API key: MISSING - set GEMINI_API_KEY in your host's Environment settings")
    else:
        print("  Gemini API key: MISSING - add GEMINI_API_KEY=your-key to the .env file")
        print("                  (no restart needed after you save it)")
    print("  Press Ctrl+C to stop.\n")

    if not on_a_host and not os.environ.get("NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.\n")


if __name__ == "__main__":
    main()
