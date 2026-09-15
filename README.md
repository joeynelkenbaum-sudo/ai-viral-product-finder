# AI Viral Product Finder

**Snap it. Scan it. Buy it.** — Upload a photo from your phone or computer (or take one
with the camera) and AI identifies the product and finds places to buy it.

## Photos and links

**A photo:** take one (phones open the native camera, desktop opens an in-page viewfinder),
choose one from the device, drag and drop, or paste with Cmd/Ctrl+V. `ImageIntake` redraws
every photo at 1280px max as JPEG before sending, which is what makes 12MP phone shots usable.

**A link:** paste it under *Or paste a link*. A browser can't read other websites, so
`server.py` visits the link and takes one image:

| Link | Image used |
| --- | --- |
| YouTube, Shorts | the video's cover image |
| TikTok | the video's cover image, from TikTok's public oEmbed |
| Shop or product page | the page's preview image (`og:image`); its title is passed to the AI as a hint |
| Direct image link | the image itself (JPG, PNG, WEBP) |

Only a video's **cover** is seen, not the video. Instagram, Facebook and some shops (often
Amazon) block automatic visits; the app says so and suggests a screenshot.

## Running it

The AI key lives on a small server (`server.py`), never in the page. Visitors use your key
without being able to see or copy it.

1. **Put your key in `.env`** (in this folder): `OPENAI_API_KEY=sk-...`. Create one at
   [platform.openai.com/api-keys](https://platform.openai.com/api-keys). The OpenAI API is
   pay-as-you-go, so the account needs billing set up.
2. **Start the server:** double-click `start-server.command`, or run `python3 server.py`.
   Your browser opens http://localhost:8000. Close the Terminal window to stop it.

Python 3 comes with macOS and the server uses only the standard library, so there is
nothing to install. You can add or change the key in `.env` while it runs.

Opening the HTML by double-click or with Live Server doesn't scan — there is no server
behind those — and the page says so. Use http://localhost:8000.

## Which AI it uses

| Plan | Scans | AI |
| --- | --- | --- |
| Free | 3 a day | Google Gemini (`GEMINI_API_KEY`), picking the best Flash model the key can use |
| Pro | 20 a day | OpenAI `gpt-5.6-luna` (`OPENAI_API_KEY`), its cheapest current model with image input ($0.20 in / $1.20 out per million tokens, September 2026); falls back to `gpt-4.1-mini` |
| Business | Unlimited (fair use: 200 a day per visitor, not shown on the page) | Same as Pro |

A scan that fails doesn't use up the day's allowance. If one key is missing, that plan uses
the other AI so the site keeps working. After `OPENAI_DAILY_CAP` OpenAI scans in a day (500
by default), paid scans switch to Gemini instead of running up the bill. `/api/health`
shows which AI each plan is using right now.

Change the limits with `FREE_SCANS_PER_DAY`, `PRO_SCANS_PER_DAY` and
`BUSINESS_SCANS_PER_DAY`, the model with `OPENAI_MODEL`, and how much it reasons with
`OPENAI_REASONING_EFFORT` (`none`, `low` by default, `medium`, `high`).

**Important while checkout is a demo:** anyone can switch to Pro without paying, and the
server can't tell who really paid, because there are no accounts yet. The daily limits and
`OPENAI_DAILY_CAP` are what keep OpenAI costs bounded until real payments exist.

## How the backend protects your key

| Protection | What it does |
| --- | --- |
| Key stays server-side | The page only sends the photo or link to `/api/identify`. The key is added by the server. |
| Nothing else is served | Only the app page and `/api/*` exist. `/.env`, `server.py` and every other file return 404. |
| Server owns the prompt | Visitors send an image or link, not instructions, so nobody can use your key as a general chatbot. The prompt is read from `IDENTIFY_PROMPT` in the HTML, so there is one copy. |
| Rate limits | Per visitor: 6 scans/minute; 3 a day on Free, 20 on Pro, 200 fair-use on Business. Failed scans are refunded. Whole site: 1,000/day, plus `OPENAI_DAILY_CAP` for OpenAI. Change with `RATE_PER_MINUTE`, `FREE_SCANS_PER_DAY`, `PRO_SCANS_PER_DAY`, `BUSINESS_SCANS_PER_DAY`, `GLOBAL_PER_DAY`. |
| Size and type checks | JPG, PNG or WEBP only, 6MB maximum. |
| Busy fallback | When the AI is overloaded or rate-limited, retries once, then moves to the next model. |
| Link safety | Only public http(s) sites on ports 80/443. Private and internal addresses, and redirects to them, are refused. The rate limit is checked before a link is visited, so the server isn't a free fetching proxy. |

Limits are kept in memory and reset when the server restarts.

## Putting it on the internet

`localhost` only works on your own computer. GitHub Pages can't run `server.py` (it only
hosts static files, so it couldn't hide the key), so the code lives on GitHub and
[Render](https://render.com) runs it. The free plan works as-is.

1. **Code on GitHub.** `.gitignore` keeps `.env` and the old split-file version out.
2. **Render:** sign up with GitHub, then **New → Blueprint** → this repository. Render reads
   `render.yaml`.
3. When it asks for keys, paste both: **`GEMINI_API_KEY`** (Free scans) and **`OPENAI_API_KEY`** (Pro and Business).
4. Deploy. The site is at `https://ai-viral-product-finder.onrender.com` (or similar).

**Already deployed?** In Render: open the service → **Environment** → add
`OPENAI_API_KEY` → **Save changes**. Render restarts and switches to OpenAI.

To update the site later, push to GitHub; Render redeploys by itself.

**Free-plan behaviour:** the service sleeps after 15 minutes without visitors, and the next
visit takes about a minute to wake it. Rate limits reset when it sleeps; the site-wide daily
cap still limits what strangers can spend.

**Before real visitors use it:** their photos are sent to the AI provider to be analyzed.
Say so in your privacy policy.

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
| 5. `STORES`, `buildSearchLinks` | Live search links on 12 stores; specialists for the product go first |
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

Optional: `buildSearchLinks()` returns search links on 12 stores (Amazon, Google Shopping, Walmart, eBay, Temu, AliExpress, Target, Etsy, Best Buy, Costco, Home Depot, Wayfair) rather than invented prices. For actual
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
