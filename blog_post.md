Building One Million Checkboxes: My Wild Journey with FastHTML, Redis Bitmaps, Modal, and 227 Commits of Pure Chaos (and Learning)A few months ago I had a silly idea: what if I built a real-time collaborative grid with exactly one million checkboxes that anyone on the internet could click — and everyone else would see the change instantly? No accounts, no login, just pure shared state. It sounded simple. It was not.The app is live right now at mtm-007--fasthtml-checkboxes-web.modal.run. Go click a few boxes. Someone halfway across the world will see them flip. That still blows my mind.This post is the full story — every bad idea, every data-loss incident, every “why is Redis eating my visitors again?” moment, and every win. Pulled straight from 227 commits, the code, the architecture diagrams, and the cold-sweat debugging sessions.The Core Idea (and Why Naïve Approaches Explode)Store one million booleans? Easy, right?
Wrong. A plain Python list or JSON blob would be ~8 MB. Every toggle and every render would hammer memory and Redis. Multiply by thousands of concurrent visitors and you’re dead.The bitmap trick that saved everything (Redis SETBIT/GETBIT/BITCOUNT):1,000,000 bits = exactly 125 KB.
SETBIT checkboxes_bitmap 42069 1 → check box #42,069
GETBIT to read
BITCOUNT for live “X of 1,000,000 checked” stats

This single insight turned an impossible memory hog into something that runs happily on a tiny Modal container.Real-Time Without WebSockets (The HTMX Polling Hack)I didn’t want to manage WebSocket connections, reconnections, or heartbeats in production. So I went full HTMX heresy:Every client polls /diffs/{client_id} every 500 ms.
When anyone toggles a box, the server adds that index to a tiny per-client diff queue.
Next poll → client gets just the changed boxes and does an OOB swap.

Latency max 500 ms, zero WebSocket state on the server, trivial scaling. I still love this decision.The Full Architecture (What Actually Ships)Frontend: Pure FastHTML + HTMX + responsive CSS (mobile-first, lazy-loads 2,000-checkbox chunks).
Backend: FastHTML (Python 3.12, async everywhere).
State: Redis bitmap (hot path) + in-memory client dict + 45-second page cache for heavy dashboards.
Analytics: Visitor tracking with geo fallback chain (ipwho.is → ipapi.co → ip-api.com), bot/VPN classification, referrer parsing (with special GitHub iframe handling), time-spent heartbeats, scroll-depth via lazy chunks, action counting.
Persistence: Modal volume + Redis RDB + SQLite backup/restore for visitor data (critical after I lost everything on the first redeploy).
Deployment: Modal serverless + GitHub Actions CI/CD (zero-downtime, auto-scaling).

(Full Mermaid diagrams are in ARCHITECTURE.md if you want the pretty pictures.)The Painful Adventures & Bugs I Fixed1. “Redis cache overwrite ate my visitor data” (multiple times)
Early versions used Redis hashes for visitors with no backup. Redeploy → container dies → Redis data gone (even with volume if snapshot timing was bad).
Fix: persistence.py now does full SQLite backup on shutdown + restore on startup. Recent commits added Starlette lifespan handlers (outside the old web_app startup/shutdown) so blog visitor records survive restarts. I literally have a commit titled “blog visitors record lost issue fixed with adding lifespan on startlette”.2. GitHub Referrer Black Hole
Traffic from the README showed as “Direct”. Root cause: GitHub renders READMEs in an iframe with referrerpolicy="no-referrer". No Referer header ever reaches the app.
Fix: UTM parameters in the README link + fallback logic in code. Now GitHub traffic correctly shows as social referral. Tiny change, huge relief.3. Mermaid Diagram Nightmares in README
I learned the hard way:### NEW comments inside Mermaid code blocks break the parser.
Parentheses in edge labels → “stadium node” syntax error.
3000–5000 ranges in Gantt charts are illegal.
→ arrows in labels cause parse failures.

Every time I updated ARCHITECTURE.md the rendered README looked broken. Many commits were “fix mermaid again”.4. Blog Visitors Tracking Saga (Feb 21–23 2026 commits)
When I added the blog post page itself with full tracking (scroll depth, actions, time spent, referrer), everything exploded:Time-spent calculation errors → temporary reset commit because math was wrong on shutdown.
Scroll data not persisting.
Plain HTML rendering vs FastHTML override bugs.
Extra parenthesis typos breaking routes.
“blog visitors history persistence on shutdown” attempts.

The final fixes: proper Starlette lifespan, heartbeat + beforeunload beacon, chunk-based scroll math. I now track how deep people read this very post.5. Refactoring Hell & Code Archaeology
I had one_M_checkboxes_old.py, random prototyping scripts, logs, database dumps scattered everywhere. Multiple commits just moving stuff into checkboxes_v0/, monetization_prop/, cleaning up, reducing line count. Early prototypes used JSON lists — I still have nightmares.6. Serverless Gotchas on Modal  Starting Redis server as subprocess inside the container.
Forcing volume commits on shutdown (volume.commit.aio()).
Making sure logs survive (logs_volume.commit).
Auto-scaling containers sharing the same Redis (client manager had to handle multiple instances gracefully).

What I’d Do Differently Next TimeSwitch to Redis Pub/Sub instead of polling (instant updates, less chatty).
Proper rate limiting per IP (Redis token bucket).
Separate analytics DB (Redis is perfect for the bitmap, overkill for visitor history).
Add Cloudflare bot management upstream.
Enforce pre-commit hooks from day one (that stray parenthesis still haunts me).

Takeaways After 227 CommitsBitmaps are magic. Any time you have millions of booleans, reach for Redis bitmaps immediately.
The Referer header is a lie. Always ship UTM parameters.
Serverless + persistent volumes is incredible but you must understand shutdown/lifespan semantics.
FastHTML + HTMX is stupidly productive. I wrote the entire frontend and backend in Python. No separate build step. Pure joy.
Persistence is never an afterthought. The SQLite backup/restore saved me more times than I can count.
Small bugs compound. One missing await, one extra parenthesis, one cache TTL that’s too aggressive — and suddenly your analytics are lying to you.

The project started as a weekend toy and turned into a masterclass in real-world system design, debugging under load, and shipping fast with modern Python tools.If you want to see the chaos for yourself:Full repo: https://github.com/mtm-007/One_Million_checboxes
ARCHITECTURE.md for diagrams
persistence.py for the backup/restore dance
main.py for the core bitmap + diff queue magic

Go click some boxes. Leave the grid a little more chaotic than you found it. And if you build something similar, drop me a link — I’d love to see what you learned.Happy hacking,
mera
(Feb 23, 2026 — after yet another “fix time spent and scroll in blog visits” commit)

