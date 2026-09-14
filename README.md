# AI Viral Product Finder

**Snap it. Scan it. Buy it.** — Upload a photo from your phone or computer (or take one
with the camera) and AI identifies the product and finds places to buy it.

## Photos only — no links

The app takes **images only**. There is no URL/link input anywhere, by design: a web page
cannot fetch or read a third-party video page (TikTok, Instagram, YouTube all block it),
so pasting a link could never produce a genuine result. The only honest input is an image
the user supplies from their own device.

Four ways to hand it a photo, all wired up:

| Way | How it works |
| --- | --- |
| **Take a photo** | On phones, opens the native camera app (`<input capture="environment">`) — reliable, correctly rotated, works without https. On desktop, opens an in-page live viewfinder via `getUserMedia`, with a front/back flip button. |
| **Choose from device** | The gallery / file picker. |
| **Drag and drop** | Drop an image anywhere on the dropzone. |
| **Paste** | Cmd+V / Ctrl+V an image from the clipboard. |

Every path funnels through `ImageIntake`, which decodes the file (honoring EXIF rotation),
redraws it on a canvas capped at 1280px on the longest side, and re-encodes it as JPEG,
stepping quality down until the payload is under 4MB. That is what makes phone photos
work — a raw 12MP shot is far too big to base64 into an API request.

## Running it

The Gemini API key lives on a small server (`server.py`), never in the page. Visitors
use your key without being able to see or copy it.

1. **Put your key in `.env`** (in this folder): `GEMINI_API_KEY=your-key`. Get one free at
   [aistudio.google.com/apikey](https://aistudio.google.com/apikey). Keys starting with
   `AQ.` work.
2. **Start the server:** double-click `start-server.command`, or run `python3 server.py`.
   Your browser opens http://localhost:8000. Close the Terminal window to stop it.

Python 3 comes with macOS and the server uses only the standard library, so there is
nothing to install. You can add or change the key in `.env` while it runs.

Opening the HTML by double-click or with Live Server no longer scans — there is no server
behind those — and the page says so. Use http://localhost:8000.

## How the backend protects your key

| Protection | What it does |
| --- | --- |
| Key stays server-side | The page only sends the photo to `/api/identify`. The key is added by the server. |
| Nothing else is served | Only the app page and `/api/*` exist. `/.env`, `server.py` and every other file return 404. |
| Server owns the prompt | Visitors send an image, not instructions, so nobody can use your key as a general chatbot. The prompt is read from `IDENTIFY_PROMPT` in the HTML, so there is one copy. |
| Rate limits | Per visitor: 6 scans/minute, 60/day. Whole site: 1,000/day, under Gemini's free quota. Change with `RATE_PER_MINUTE`, `RATE_PER_DAY`, `GLOBAL_PER_DAY`. |
| Size and type checks | JPG, PNG or WEBP only, 6MB maximum. Links are refused. |
| Busy-model fallback | On Google's 503/429, retries, drops JSON mode, then moves to the next-best model. |

Limits are kept in memory and reset when the server restarts.

## Putting it on the internet

`localhost` only works on your own computer. GitHub Pages cannot run `server.py` (it only
hosts static files, so it could not hide the key), so the code goes on GitHub and
[Render](https://render.com) runs it. The free plan works as-is.

1. **Code on GitHub.** `.gitignore` keeps `.env` and the old split-file version out.
   Check that `.env` is not in the repository.
2. **Render account:** sign up at render.com with *GitHub*.
3. **New → Blueprint** → pick this repository. Render reads `render.yaml`, which already
   says how to start the server and that nothing needs installing.
4. When it asks for **`GEMINI_API_KEY`**, paste your key. It is stored as a secret on
   Render, never in the code.
5. Deploy. Your site is at `https://ai-viral-product-finder.onrender.com` (or similar).

To update the site later, push to GitHub; Render redeploys by itself.

**Free-plan behaviour:** the service sleeps after 15 minutes without visitors, and the next
visit takes about a minute to wake it. Rate limits reset when it sleeps. The whole-site
daily cap still protects your Gemini quota.

**Before real visitors use it:** on Gemini's free tier, Google may use submitted photos to
improve its products and human reviewers may read them
([terms](https://ai.google.dev/gemini-api/terms)). Say so in your privacy policy, or move
the key to a paid Gemini project, where that does not happen.

### In-browser key mode (old)

Set `CONFIG.backendEndpoint` to `null` in the HTML to go back to visitors pasting their own
Google or OpenAI key into the page. The server is not needed in that mode.

## Code layout

The whole app is one HTML file with a `<style>` block and a `<script>` block. The script is
in numbered sections:

| Section | What it does |
| --- | --- |
| 1. `CONFIG` | The few values worth changing |
| 2. Utilities | `escapeHtml`, safe `store` wrapper, byte formatting |
| 3. `ImageIntake` | File/camera → normalized, downscaled JPEG data URL |
| 4. `IdentifyService` | The vision call — direct to OpenAI, or via your backend |
| 5. `buildSearchLinks` | Real Amazon/Etsy/Google/Walmart search URLs |
| 6. `PLANS`, `SubscriptionService`, `PaymentService` | Plans, metering, checkout |
| 7. Icons | Inline SVG constants |
| 8. State + `setView` | The single place screens swap and the camera is torn down |
| 9. Views | `renderHome`, `renderPricing`, upgrade modal, usage pill |
| 10. `bindHomeEvents` | All home-screen wiring |
| 11. Scan flow | `startScan`, `renderScanning`, `renderResult`, `renderError` |
| 12. Global listeners + init | Registered once, so re-renders never stack handlers |

## Subscription

| Plan | Price | Scans |
| --- | --- | --- |
| Free | $0 | 3/month |
| Pro | $9/mo · $79/yr | Unlimited |
| Business | $29/mo · $279/yr | Unlimited + bulk, CSV, API |

Usage is metered in `localStorage` and resets every 30 days. A scan only costs a credit
when it actually succeeds — failures are free.

## Before going live

Three things are demo-only. Each is isolated to one place:

1. **Key server-side** — done: see *How the backend protects your key*.
2. **Real payments** — replace the simulated delay in `PaymentService.checkout()` with a
   Stripe Checkout session, and activate the plan from the `checkout.session.completed`
   webhook on your server, never from the browser.
3. **Server-side metering** — `SubscriptionService.load()` reads `localStorage`, so users
   can reset their own scan count. Replace it with a fetch to `/api/me`.

Optional: `buildSearchLinks()` returns search URLs rather than invented prices. For actual
listings with prices and ratings, plug a shopping API (SerpApi, Rainforest, Amazon PA-API)
in there.

## Stale files

`index.html` only forwards Live Server to the app. `index (3).html`, `app.js`, and `styles.css` are an older split-file version of this app.
They still contain the removed link/demo mode, and `index (3).html` points at `css/` and
`js/` paths that do not exist in this folder, so that version does not run. The standalone
HTML file is the one that works. Delete the other three when you are sure you do not want
them.

## Browser support

Modern evergreen browsers. Uses `fetch`, `createImageBitmap` (with a fallback),
`getUserMedia`, canvas, CSS custom properties, and `aspect-ratio`. Respects
`prefers-reduced-motion`.
