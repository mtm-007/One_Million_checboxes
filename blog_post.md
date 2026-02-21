# Building One Million Checkboxes: A Real-Time Collaborative App with FastHTML, Redis & Modal

A few weeks ago I built and deployed a real-time collaborative web app where anyone in the world can click any of one million checkboxes — and everyone else sees it update instantly. Along the way I learned a lot about architecture, serverless deployment, visitor analytics, and the surprisingly tricky world of referrer tracking. This post walks through the whole journey.

---

## The Idea

The concept is simple: one million checkboxes, shared state, live updates. Click one and every connected browser reflects the change in under a second. It sounds trivial until you think about the data — naively storing a million booleans as a JSON list costs around 8MB in memory. Multiply that by every read and write and it gets expensive fast.

The app is live at [mtm-007--fasthtml-checkboxes-web.modal.run](https://mtm-007--fasthtml-checkboxes-web.modal.run/) if you want to try it before reading further.

---

## The Stack

I chose four tools that worked together cleanly:

**FastHTML** — a Pythonic web framework that lets you build full-stack apps without leaving Python. No Jinja templates, no separate frontend build step. Components are just Python functions.

**HTMX** — lightweight JavaScript that handles interactivity through HTML attributes. Polling, lazy loading, out-of-band swaps — all without writing a single line of custom JS for the core UI.

**Redis** — the backbone of the whole thing. Not just for caching but as the primary data store for checkboxes, visitor data, geolocation cache, and page cache.

**Modal** — serverless hosting with auto-scaling and persistent volumes. CI/CD with GitHub Actions deploys straight to Modal on every push.

---

## The Architecture

### The Bitmap Trick

The key insight that made this feasible is Redis bitmaps. Instead of storing a million booleans as a list or JSON, Redis lets you address individual bits inside a string. One million bits = **125KB**. That's it. Compare that to 8MB for a JSON list — a 64x reduction.

```
SETBIT checkboxes_bitmap 42 1   → check box #42
GETBIT checkboxes_bitmap 42     → read box #42
BITCOUNT checkboxes_bitmap      → count all checked boxes
```

Every toggle is a single Redis command. Every read is a single Redis command. The entire state of one million checkboxes fits in a single Redis key.

### Real-Time Updates Without WebSockets

Rather than setting up WebSockets, I used HTMX polling. Every browser polls `/diffs/{client_id}` every 500ms. The server maintains a diff queue per client — when any checkbox is toggled, that change gets added to every other client's queue. On the next poll, the client gets back just the changed checkboxes and updates them in place.

The flow looks like this:

1. User clicks checkbox #42
2. Browser POSTs to `/toggle/42/{client_id}`
3. Server runs `GETBIT`, then `SETBIT` on the Redis bitmap
4. Server adds `#42` to every other client's diff queue
5. Server runs `BITCOUNT` and returns updated stats to the clicking browser
6. Other browsers hit `/diffs/{client_id}` on their next 500ms poll
7. Server returns the queued diff, browsers update checkbox #42

This approach trades some latency (up to 500ms) for simplicity. No WebSocket infrastructure, no connection management, no reconnection logic.

### System Design Overview

```
👥 CLIENT LAYER
   Browsers (HTMX + Responsive CSS)
        ↓ HTTP/HTMX
🖥️ APPLICATION LAYER
   FastHTML Web Server
   ├── Routes / Handlers      → GETBIT/SETBIT/BITCOUNT on Bitmap
   ├── Client Manager         → Diff queues per connected client
   ├── Geo API Layer          → Fallback chain: ipwho.is → ipapi.co → ip-api.com
   └── Redis Cache Layer      → 45s TTL for heavy pages like /visitors

💾 DATA LAYER (Redis)
   ├── Bitmap (125KB)         → All 1M checkbox states
   ├── Visitor Data           → Hash + Sorted Set per IP
   ├── Geolocation Cache      → Avoids repeat API calls
   └── Page Cache             → Pre-computed dashboard HTML

💿 STORAGE LAYER
   Modal Volume               → /data/dump.rdb + SQLite for persistence
```

---

## Building the Visitor Analytics Dashboard

Once the core checkbox functionality was working I wanted to know who was using it. I built a full visitor tracking system that captures:

- IP address and geolocation (city, country, ISP, ZIP)
- Device type and OS (parsed from User-Agent)
- Classification: Human, Bot, VPN user, or Relay
- Referrer source and type (direct, social, search, referral)
- Time spent per session, scroll depth, page views, actions
- First and last referrer per visitor

### Visitor Tracking Flow

```
New Visitor arrives
      ↓
Extract IP (CF-Connecting-IP header)
      ↓
Check Redis geo cache
      ├── Cache hit  → use cached geo data
      └── Cache miss → try ipwho.is → ipapi.co → ip-api.com (fallback chain)
                             ↓
                       Save to Redis geo cache
      ↓
Classify visitor (Human / Bot / VPN / Relay)
      ↓
Save/update visitor:{ip} hash in Redis
      ↓
Add to recent_visitors_sorted (sorted set by timestamp)
      ↓
Increment total_visitors_count (if new visitor)
      ↓
Save to SQLite for persistence across Redis restarts
```

The classification logic checks the User-Agent against a known bots dictionary, flags hosting provider IPs as Bot/Server, and checks the geo API response for VPN and relay flags.

### Session Tracking

Beyond just recording visits, I track how long people actually engage. A JavaScript tracker sends heartbeats every 10 seconds while the page is open, and fires a final `session-end` beacon on `beforeunload` using `navigator.sendBeacon` so it doesn't get cancelled when the tab closes.

Scroll depth is tracked by counting how many lazy-loaded chunks of checkboxes the user has triggered — since the grid loads in 500-chunk batches of 2,000 checkboxes each, I can calculate percentage scroll depth from chunk load count.

---

## The Page Cache Layer

The `/visitors` dashboard is expensive to compute. It aggregates data across hundreds of visitor records, groups by day, computes chart data, calculates stats. Doing that on every request would be slow and wasteful.

I added a Redis page cache layer with a 45-second TTL:

```python
cache_key = f"cache:visitors:{offset}:{limit}:{days}"
cached = await redis.get(cache_key)
if cached:
    return json.loads(cached), "cache-hit"

# ... expensive computation ...

await redis.set(cache_key, json.dumps(result), ex=45)
return result, "cache-miss"
```

Cache hits return in ~10ms. Cache misses take 3-5 seconds. For a dashboard that updates every 45 seconds anyway, this is a great tradeoff.

---

## Debugging the GitHub Referrer Problem

After launching, I noticed that visitors coming from my GitHub README weren't showing up as GitHub referrals — they were all showing as Direct traffic. This was frustrating since I had added referrer tracking specifically to understand where traffic was coming from.

The root cause: **GitHub renders READMEs inside an iframe with `referrerpolicy="no-referrer"`**. This means the browser deliberately sends no `Referer` header when someone clicks a link from a GitHub README. There's nothing to track.

The fix is UTM parameters. Instead of relying on the `Referer` header, embed tracking info directly in the URL:

```
https://myapp.com/?utm_source=github&utm_medium=readme&utm_campaign=one-million-checkboxes
```

Then in the server code, fall back to UTM params when the header is empty:

```python
referrer = request.headers.get('referer', '')

# UTM fallback — GitHub strips the Referer header
if not referrer and request.query_params.get('utm_source') == 'github':
    referrer = 'https://github.com'
```

Once `https://github.com` is reconstructed as the referrer, the existing `parse_referrer` function picks it up correctly — as long as `github.com` is in your social platforms dictionary:

```python
social_platforms = {
    ...
    "github.com": "GitHub",
}
```

Two small changes, and GitHub traffic now shows up correctly as a social referral source.

---

## README Rendering Issues

Documenting the architecture in the README with Mermaid diagrams introduced its own set of bugs. A few things I learned the hard way:

**Inline `### comments` break Mermaid.** I was annotating new features with `### NEW` inline in diagram code blocks. Mermaid has no concept of inline comments — `###` starts a heading in Markdown but means nothing to the Mermaid parser, which just chokes on it. Fix: remove them entirely or use `%%` Mermaid comments on their own line.

**Parentheses in edge labels break graph diagrams.** This line:

```
PageCache -->|Ephemeral (optional persist)| Disk
```

...caused a parse error because the Mermaid parser sees `(` as the start of a stadium-shaped node. Fix: replace parentheses with a dash.

```
PageCache -->|Ephemeral - optional persist| Disk
```

**Gantt charts need single numeric end values.** I had written `3000–5000` as a range for a task duration. The `dateFormat X` gantt mode expects a single integer. Fix: pick one number.

**Special characters in gantt labels cause issues.** The `→` arrow character in task labels like `Cache Hit → Fast Render` can trip up the parser. Fix: replace with `-`.

---

## CI/CD with GitHub Actions

Every push to main triggers a GitHub Actions workflow that deploys directly to Modal. The pipeline runs tests, then calls `modal deploy` with the production app. Modal handles spinning up new containers, draining old ones, and keeping the persistent volume mounted throughout. Zero-downtime deploys with no manual steps.

---

## What I'd Do Differently

**Use Redis Pub/Sub instead of polling.** The 500ms poll interval works fine but it's chatty. Pub/Sub would push updates to clients immediately and reduce unnecessary requests when nothing has changed.

**Add rate limiting per IP.** Right now anyone can spam toggles. A simple Redis-based token bucket per IP would prevent abuse.

**Separate the analytics database.** Using Redis for both the hot checkbox state and the analytics data means they share memory. For production I'd move visitor data to a proper database and keep Redis purely for the real-time checkbox state.

**Better bot filtering upstream.** Most of my "visitors" are crawlers and bots. Cloudflare's bot management or even a simple honeypot field would clean up the analytics significantly.

---

## Takeaways

Building this taught me a few things worth remembering:

The **bitmap trick** is genuinely useful any time you need to track large sets of boolean states efficiently. 125KB vs 8MB is not a marginal improvement.

**UTM parameters are not optional** if you care about referrer attribution. The `Referer` header is unreliable — stripped by privacy browsers, iframe policies, HTTPS-to-HTTP transitions. Build UTM tracking from day one.

**Mermaid diagrams in READMEs are worth the effort** but have real parser quirks. Test them locally with a Mermaid live editor before committing, especially if you're adding special characters or inline annotations.

**Modal + FastHTML is a genuinely fast way to ship Python web apps.** No Dockerfiles, no infrastructure config, just Python and a deploy command.

---

The full source code is on GitHub. If you want to dig into the bitmap implementation, the visitor tracking pipeline, or the HTMX polling pattern, everything is there.

Give the checkboxes a click. Someone on the other side of the world will see it.