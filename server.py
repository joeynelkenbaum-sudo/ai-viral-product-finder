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
import http.client
import ipaddress
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urljoin, urlparse

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

# Links: the server fetches one image on the visitor's behalf.
MAX_LINK_LENGTH = 2048
MAX_PAGE_BYTES = 2 * 1024 * 1024
LINK_TIMEOUT_SECONDS = 12
LINK_USER_AGENT = "Mozilla/5.0 (compatible; AIViralProductFinder/1.0; +https://ai-viral-product-finder.onrender.com)"
YOUTUBE_THUMBNAIL_URL = "https://i.ytimg.com/vi/%s/%s"
TIKTOK_OEMBED_URL = "https://www.tiktok.com/oembed?url="


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


def identify(image_data_url, key, refreshed=False, context=""):
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
    if context:
        # A page or video title helps a lot ("Stanley Quencher H2.0 40oz..."), but
        # strangers write those, so it is offered as a hint, never as an instruction.
        prompt += ("\n\nThe image came from a web page or video titled: \"%s\". Use that only as a hint: "
                   "trust what you can see in the image, and still reply in the JSON shape above."
                   % context[:200].replace('"', "'"))
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
        return identify(image_data_url, key, refreshed=True, context=context)
    raise last_error


# --------------------------------------------------------------------------
# Links
# A browser can't read TikTok or a shop page, but this server can. From a link
# we take ONE image - a video's cover, a page's preview image, or the image the
# link points at - and identify it exactly like an uploaded photo.
# --------------------------------------------------------------------------

def is_host(host, domain):
    return host == domain or host.endswith("." + domain)


def assert_public_url(url):
    """
    Refuse anything that isn't an ordinary public website. Without this a
    visitor could make the server fetch addresses inside Render's own network
    (server-side request forgery).
    """
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ClientError(400, "Paste a full link that starts with https://")
    try:
        port = parts.port
    except ValueError:
        raise ClientError(400, "That link isn't valid.")
    if port not in (None, 80, 443):
        raise ClientError(400, "That link uses an unusual port and can't be opened.")
    try:
        addresses = socket.getaddrinfo(parts.hostname, port or 443, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise ClientError(422, "That website couldn't be found. Check the link.")
    for address in addresses:
        if not ipaddress.ip_address(address[4][0].split("%")[0]).is_global:
            raise ClientError(400, "That link points to a private network address and can't be opened.")


class CheckedRedirects(urllib.request.HTTPRedirectHandler):
    """Re-check every hop: a public page can redirect to a private address."""
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        assert_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


LINK_OPENER = urllib.request.build_opener(CheckedRedirects)


def fetch_link(url, max_bytes):
    """GET a public URL. Returns (final_url, content_type, body); raises ClientError."""
    assert_public_url(url)
    request = urllib.request.Request(url, headers={
        "User-Agent": LINK_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,image/webp,image/*;q=0.9,*/*;q=0.5",
        "Accept-Language": "en-US,en;q=0.8",
    })
    try:
        with LINK_OPENER.open(request, timeout=LINK_TIMEOUT_SECONDS) as response:
            return response.geturl(), response.headers.get("Content-Type", ""), response.read(max_bytes + 1)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403, 429, 999):
            raise ClientError(422, "That website blocks automatic visits, so the link can't be read. "
                                   "Take a screenshot and upload it instead.")
        if error.code in (404, 410):
            raise ClientError(422, "That link leads to a page that doesn't exist. Check it and try again.")
        raise ClientError(422, "That website returned an error (%s). Try a screenshot instead." % error.code)
    except ClientError:
        raise
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        raise ClientError(422, "That link couldn't be opened. Check it, or take a screenshot and upload it instead.")


def sniff_image(data):
    """Trust the bytes, not the Content-Type header."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def as_data_url(data):
    if len(data) > MAX_IMAGE_BYTES:
        raise ClientError(422, "The image behind that link is larger than 6MB.")
    mime = sniff_image(data)
    if not mime:
        raise ClientError(422, "The image behind that link isn't a JPG, PNG or WEBP.")
    return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))


def fetch_image(url):
    return as_data_url(fetch_link(url, MAX_IMAGE_BYTES)[2])


class PagePreview(HTMLParser):
    """The preview image and title a page offers to link-sharing apps."""
    IMAGE_KEYS = ("og:image:secure_url", "og:image", "og:image:url", "twitter:image", "twitter:image:src", "image_src")
    TITLE_KEYS = ("og:title", "twitter:title")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.page_title = ""
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = {name.lower(): (value or "").strip() for name, value in attrs}
        if tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            if key and attrs.get("content"):
                self.meta.setdefault(key, attrs["content"])
        elif tag == "link" and "image_src" in attrs.get("rel", "").lower().split() and attrs.get("href"):
            self.meta.setdefault("image_src", attrs["href"])
        elif tag == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title and len(self.page_title) < 300:
            self.page_title += data

    def image(self):
        return next((self.meta[key] for key in self.IMAGE_KEYS if self.meta.get(key)), None)

    def title(self):
        found = next((self.meta[key] for key in self.TITLE_KEYS if self.meta.get(key)), self.page_title)
        return " ".join(found.split())


def youtube_video_id(parts):
    host = (parts.hostname or "").lower()
    candidate = ""
    if is_host(host, "youtu.be"):
        candidate = parts.path.strip("/").split("/")[0]
    elif is_host(host, "youtube.com") or is_host(host, "youtube-nocookie.com"):
        if parts.path.rstrip("/") == "/watch":
            candidate = parse_qs(parts.query).get("v", [""])[0]
        else:
            match = re.match(r"^/(?:shorts|embed|live|v)/([^/?#]+)", parts.path)
            candidate = match.group(1) if match else ""
    return candidate if re.match(r"^[A-Za-z0-9_-]{11}$", candidate) else None


def normalise_link(text):
    link = str(text or "").strip()
    if len(link) > MAX_LINK_LENGTH:
        raise ClientError(400, "That link is too long.")
    if not re.match(r"^https?://", link, re.I):
        if not re.match(r"^[\w-]+(\.[\w-]+)+(/|$)", link):
            raise ClientError(400, "Paste a full link, like https://www.tiktok.com/...")
        link = "https://" + link
    return link


def link_to_image(text):
    """Turn a pasted link into (image_data_url, source) where source says where it came from."""
    link = normalise_link(text)
    parts = urlparse(link)
    host = (parts.hostname or "").lower()

    video = youtube_video_id(parts)
    if video:
        for size in ("maxresdefault.jpg", "hqdefault.jpg"):   # the large cover doesn't exist for every video
            try:
                return fetch_image(YOUTUBE_THUMBNAIL_URL % (video, size)), {"kind": "video-cover", "host": "YouTube", "title": ""}
            except ClientError:
                continue
        raise ClientError(422, "Couldn't get that YouTube video's cover image. Check the link.")

    if is_host(host, "tiktok.com"):
        target = link
        if host.split(".")[0] in ("vm", "vt"):   # short share links redirect to the real video
            target = fetch_link(link, 256 * 1024)[0]
        try:
            info = json.loads(fetch_link(TIKTOK_OEMBED_URL + quote(target, safe=""), 256 * 1024)[2])
        except ValueError:
            info = {}
        if not isinstance(info, dict) or not info.get("thumbnail_url"):
            raise ClientError(422, "TikTok didn't share that video's cover image. "
                                   "Take a screenshot of the video and upload it instead.")
        return fetch_image(info["thumbnail_url"]), {"kind": "video-cover", "host": "TikTok",
                                                     "title": str(info.get("title", ""))[:200]}

    final_url, content_type, body = fetch_link(link, MAX_IMAGE_BYTES)
    if sniff_image(body):
        return as_data_url(body), {"kind": "image", "host": host, "title": ""}

    page_text = body[:MAX_PAGE_BYTES].decode("utf-8", "replace")
    if "html" not in content_type.lower() and "<html" not in page_text[:2000].lower():
        raise ClientError(422, "That link isn't a web page or a JPG, PNG or WEBP image.")
    page = PagePreview()
    try:
        page.feed(page_text)
    except Exception:   # a broken page may still have given us its meta tags
        pass
    image = page.image()
    if not image:
        if is_host(host, "instagram.com") or is_host(host, "facebook.com"):
            raise ClientError(422, "Instagram and Facebook don't let other sites read their posts. "
                                   "Take a screenshot and upload it instead.")
        # Seen with Amazon: a 200 page with no image at all is often a bot check,
        # not a page that genuinely lacks one - so don't claim to know which.
        raise ClientError(422, "We couldn't find a product image on that page. Some shops, like Amazon, "
                               "hide it from automatic visits. Take a screenshot of the product and upload it instead.")
    shown_host = host[4:] if host.startswith("www.") else host
    return fetch_image(urljoin(final_url, image)), {"kind": "page-image", "host": shown_host, "title": page.title()[:200]}


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
        if not getattr(self, "head_only", False):
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

    def do_HEAD(self):
        # Uptime monitors and link previews (WhatsApp, iMessage...) often ask
        # with HEAD: same headers as GET, no body.
        self.head_only = True
        try:
            self.do_GET()
        finally:
            self.head_only = False

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
        link = body.get("url") if isinstance(body, dict) else None
        if not image and not link:
            raise ClientError(400, "Send a photo or a link.")

        # Checked before any link is visited, so the server can't be used as a
        # free web-fetching proxy either.
        limited = LIMITER.check(self.client_ip())
        if limited:
            raise ClientError(429, limited)

        if image:
            return identify(image, key)

        image, source = link_to_image(link)
        product = identify(image, key, context=source.get("title", ""))
        product["source"] = dict(source, image=image)
        return product


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
