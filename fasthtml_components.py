import fasthtml.common as fh

def stat_card(label, value, subtitle=""):
    return fh.Div(fh.Div(label, cls="stats-label"), fh.Div(value, cls="stats-number"),
                  fh.Div(subtitle, style="font-size:0.9em;opacity:0.8;") if subtitle else "", cls="stats-card")

def h_bar(label, count, total, color="#667eea"):
    """Single horizontal bar"""
    pct = (count / total * 100) if total > 0 else 0
    return fh.Div(fh.Span(label, cls="bar-label-horizontal"), 
                  fh.Div(fh.Div(fh.Span(f"{count} ({pct:.1f}%)" if count > 0 else "", 
                  style="color:white;font-size:0.9em;padding-left:8px;"),
                  style=f"width:{max(pct,2) if count>0 else 0}%;background:{color};", cls="bar-fill-horizontal"),
                  cls="bar-track-horizontal"), cls="bar-horizontal")

def h_chart(data, colors=None):
    """Horizontal bar chart"""
    data = dict(data) if isinstance(data, list) else data
    total = sum(data.values())
    return fh.Div(*[h_bar(k, v, total, colors.get(k, "#667eea") if colors else "#667eea") 
                    for k, v in data.items()], cls="chart-bars-container") if total else fh.P("No data", style="text-align:center;color:#999;")

def gradient_chart(data, grad="linear-gradient(90deg,#667eea 0%,#764ba2 100%)"):
    """Gradient bar chart for time series"""
    if not data: return fh.P("No data", style="text-align:center;color:#999;")
    mx = max([c[1] for c in data], default=1)
    return fh.Div(*[fh.Div(fh.Span(lbl, cls="bar-label-horizontal"),
                    fh.Div(fh.Div(fh.Span(f"{cnt}" if cnt>0 else "", style="color:white;font-size:0.8em;padding-left:8px;"),
                    style=f"width:{max((cnt/mx*100),2) if cnt>0 else 0}%;background:{grad};", cls="bar-fill-horizontal"),
                    cls="bar-track-horizontal"), cls="bar-horizontal") for lbl, cnt in data], cls="chart-bars-container")

def nav_links(*links):
    return fh.Div(*[fh.A(txt, href=url, cls="back-link", 
                    style=f"{'margin-left:20px;' if i else ''}{rest[0] if rest else ''}") 
                    for i, (txt,url,*rest) in enumerate(links)], style="text-align:center;margin-top:30px;")

def sec_badge(vpn, relay):
    return fh.Span("iCloud Relay" if relay else "VPN/PROXY" if vpn else "Clean", 
                   style=f"background:{'#5856d6' if relay else '#ff3b30' if vpn else '#4cd964'};color:white;padding:2px 6px;border-radius:4px;font-size:0.8em;")

def class_badge(cls):
    h = "Human" in cls
    return fh.Span(f"{'👤' if h else '🤖'} {cls}", 
                   style=f"background:{'rgba(16,185,129,0.15)' if h else 'rgba(245,158,11,0.15)'};color:{'#10b981' if h else '#f59e0b'};padding:4px 8px;border-radius:4px;font-weight:600;font-size:0.85em;")

def ref_badge(src, typ):
    return fh.Span(src[:20], style=f"background:{ {'direct':'#95a5a6','social':'#ff6b6b','search':'#4ecdc4','referral':'#45b7d1'}.get(typ,'#999')};"
                "color:white;padding:2px 6px;border-radius:4px;font-size:0.8em;" )

def pagination(offset, limit, total, url, extra=None):
    more = (offset + limit) < total
    def build(o): return f"{url}?offset={o}&limit={limit}" + (f"&{'&'.join(f'{k}={v}' for k,v in extra.items())}" if extra else "")
    return fh.Div(fh.Div(
        fh.A("← Prev", href=build(max(0,offset-limit)), cls="pagination-btn") if offset>0 else fh.Span("← Prev", cls="pagination-btn disabled"),
        fh.Span(f"Showing {offset+1}-{min(offset+limit,total)} of {total}", cls="pagination-info"),
        fh.A("Next →", href=build(offset+limit), cls="pagination-btn") if more else fh.Span("Next →", cls="pagination-btn disabled"),
        cls="pagination-controls"), fh.Div(fh.Span("Show: ", style="margin-right:10px;"),
        *[fh.A(str(l), href=f"{url}?offset=0&limit={l}" + (f"&{'&'.join(f'{k}={v}' for k,v in extra.items())}" if extra else ""),
        cls=f"limit-btn{' active' if limit==l else ''}") for l in [50,100,200,500]], cls="limit-controls"), cls="pagination-wrapper")

def range_sel(curr, limit, offset, url):
    return fh.Div(fh.Span("Chart Range: ", style="margin-right:10px;font-weight:bold;color:#667eea;"),
                  *[fh.A(str(d), href=f"{url}?days={d}&limit={limit}&offset={offset}", 
                  cls=f"range-btn{' active' if curr==d else ''}", title=f"Last {d} days") for d in [7,14,30]], cls="range-selector")

def fmt_time(s): 
    return f"{s:.0f}s" if s<60 else f"{s/60:.1f}m" if s<3600 else f"{s/3600:.1f}h"
