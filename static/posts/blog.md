---
title: "Building One Million Checkboxes site"
description: "Welcome to my first blog post! How I build multi-player checkboxes site with users analytics"
author: "Merhawi"
date: "2026-02-26"
categories: ["Blog", "Table of Contents", "Introduction"]
image: "https://picsum.photos/400/200?random=10"
---

# Building One Million Checkboxes with Analytics
## Introduction

As a junior Machine Learning Engineer, my world had been PyTorch, datasets, training loops, and model checkpoints — zero frontend experience. HTML? CSS? JavaScript? Barely touched them. But I wanted to build something interactive and live for my upcoming diffusion models site (and a few other ideas brewing), so I decided to force myself into the deep end. This "One Million Checkboxes" project was my deliberate hack-around: a fun, absurd excuse to learn real frontend + full-stack deployment skills while shipping something shareable. What started as a side quest turned into one of the best learning explosions I've had.

A few months ago the silly idea hit: build a real-time collaborative grid with *exactly one million checkboxes* — anyone clicks, everyone sees it instantly. No login, no accounts, pure shared chaos. It sounded simple. It absolutely was not.

The app is live right now at [mtm-007--fasthtml-checkboxes-web.modal.run](https://mtm-007--fasthtml-checkboxes-web.modal.run/). Go click a few. Someone across the world will see them flip. That still blows my mind — and now I actually understand how to make it happen.

This is the full story — every terrible idea, every data-loss panic, every "why is Redis eating my visitors again?", pulled straight from 227 commits, the code, `ARCHITECTURE.md` diagrams, and many cold-sweat debugging nights.


## The Core Idea (and Why Naïve Approaches Explode)

Store one million booleans? Easy, right? **Wrong.** A Python list or JSON blob ≈ 8 MB. Every toggle and render would murder memory and Redis. Thousands of concurrent visitors? Dead server.

### The Bitmap Trick That Saved Everything

Redis bitmaps: 1,000,000 bits = **exactly 125 KB**.
```
SETBIT checkboxes_bitmap 42069 1   → check #42,069
GETBIT checkboxes_bitmap 42069     → read it
BITCOUNT checkboxes_bitmap         → live "X / 1,000,000 checked" stats
```

One key, one command per toggle/read, tiny memory. This turned impossible into "runs happily on tiny Modal container".

## Real-Time Without WebSockets (The HTMX Polling Hack)

No WebSocket state, reconnections, heartbeats in prod. Instead: HTMX heresy.

- Every client polls `/diffs/{client_id}` every 500 ms
- Toggle → server adds index to per-client diff queues
- Next poll → client gets only changes, OOB swap

Max 500 ms latency, zero server connection management, scales trivially. Still love this choice — and HTMX became one of my favorite discoveries for future projects.

## The Full Architecture (What Actually Ships)

- **Frontend**: FastHTML + HTMX + responsive CSS (mobile-first, lazy-load 2,000-checkbox chunks)
- **Backend**: FastHTML (Python 3.12, async) built on Starlette
- **State**: Redis bitmap (hot path) + in-memory client dict + 45s page cache for dashboards
- **Analytics**: Geo fallback (ipwho.is → ipapi.co → ip-api.com), bot/VPN classification, referrer parsing (GitHub iframe fix), time-spent heartbeats, scroll-depth, actions
- **Persistence**: Modal volume + Redis RDB + SQLite backup/restore (saved me repeatedly)
- **Deployment**: Modal serverless + GitHub Actions CI/CD (zero-downtime auto-scaling)

## The Painful Adventures & Bugs I Fixed

**1. "Redis overwrite ate my visitor data" (multiple times)**
Early Redis hashes, no backup. Redeploy → container dies → gone. Fix: `persistence.py` SQLite backup on shutdown + restore on startup + Starlette lifespan handlers.

**2. GitHub Referrer Black Hole**
README traffic showed "Direct" because of GitHub's `referrerpolicy="no-referrer"` iframe. Fix: UTM fallback logic.

**3. Mermaid Diagram Nightmares in README**
Comments, parentheses, arrows, en-dashes — all broke the parser. Dozens of "fix mermaid again" commits.

**4. Blog Visitors Tracking Saga (Feb 21–23 2026)**
Time-spent math errors, scroll not persisting, shutdown loss. Fixed with proper lifespan, heartbeat + beforeunload beacon, chunk-based scroll math.

**5. Refactoring Hell & Code Archaeology**
Scattered `one_M_checkboxes_old.py`, prototypes, logs, DB dumps. Moved everything into `checkboxes_v0/` and `monetization_prop/`, deleted dead code, reduced line count dramatically.

**6. Serverless Gotchas on Modal**
Redis as subprocess, forced volume commits, multi-container Redis sharing, scaling from 1→3 containers.

**7. FastHTML vs Plain HTML Rendering War**
Wanted beautiful serif blog with zero interference. Main site's `style_v2.css` + FastHTML injection wrecked fonts/layout. Fix: Pure Starlette bypass for blog route — raw `Response(content=raw_html, media_type="text/html")`.

**8. Modal App Logs Nightmare**
Modal log viewer painful for real debugging. Fix: Dedicated `/data/app.log` on volume + extractor script.

## What I'd Do Differently Next Time

- Redis Pub/Sub over polling → instant, less chatty
- Rate limiting (Redis token bucket per IP)
- Separate analytics DB
- Cloudflare bot management upstream
- Pre-commit hooks + structured logging from day one
- Decide early: full FastHTML or pure Starlette for clean/static pages

## Takeaways After 227 Commits

- **Bitmaps are magic**. Millions of booleans? Redis bitmaps first.
- **Referer header lies**. Ship UTM always.
- **Serverless + volumes incredible**, but master shutdown/lifespan + explicit file logging.
- **FastHTML + HTMX stupidly productive** — but know when to drop to raw Starlette for pixel-perfect HTML.
- **Persistence & logging are never afterthoughts**. SQLite + volume log file saved me repeatedly.
- **Small bugs compound**. Missing await, extra parenthesis, CSS bleed, aggressive TTL → lying analytics.
- **Jump in even if you're a total beginner on one side**. Coming from pure ML, this forced me to learn frontend deployment end-to-end.

Started as weekend toy to level up my skills, became a masterclass in system design, load debugging, framework boundaries, and shipping fast with modern Python tools.

See the chaos:
- Repo: [github.com/mtm-007/One_Million_checboxes](https://github.com/mtm-007/One_Million_checboxes)
- `ARCHITECTURE.md` for all diagrams
Go click some boxes. Leave it more chaotic.

Happy hacking,
mera
