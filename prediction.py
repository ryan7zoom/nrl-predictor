#!/usr/bin/env python3
"""
NRL Signal — fetches Alphr + Stats Insider NRL predictions, cross-matches
picks where both sources agree, and writes docs/index.html directly.
No separate CSS/JS files — everything inlined so nothing can go missing.
"""

import json
import re
import urllib.request
from datetime import datetime, timezone, timedelta

ALPHR_URL = "https://alphr.com.au/api/predictions/weekly"
SI_URL = (
    "https://levy-edge.statsinsider.com.au/matches/upcoming"
    "?Sport=NRL&strip=true&best_bets=true"
    "&bookmakers=bet365,bluebet,betfair,tab,pointsbet_au"
)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; nrl-signal-bot/1.0)"}


def fetch_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Team-name normalisation so Alphr's full names match Stats Insider's abvs
# ---------------------------------------------------------------------------

TEAM_ALIASES = {
    "titans": "GLD", "cowboys": "NQL", "warriors": "WAR", "panthers": "PEN",
    "roosters": "SYD", "bulldogs": "CBY", "storm": "MEL", "sea eagles": "MAN",
    "dolphins": "DOL", "broncos": "BRI", "rabbitohs": "SOU", "eels": "PAR",
    "raiders": "CAN", "knights": "NEW", "dragons": "SGI", "sharks": "CRO",
    "wests tigers": "WST",
}


def norm_team(name):
    return TEAM_ALIASES.get(name.strip().lower(), name.strip().upper()[:3])


def match_key(home, away):
    return "-".join(sorted([norm_team(home), norm_team(away)]))


# ---------------------------------------------------------------------------
# Alphr: pull out H2H / Line(MBand) / Total picks per match
# ---------------------------------------------------------------------------

def get_alphr_predictions():
    data = fetch_json(ALPHR_URL)
    out = {}
    for sport_block in data.get("sports", []):
        if sport_block.get("sport") != "nrl":
            continue
        for match in sport_block.get("matches", []):
            home, away = match["home_team"], match["away_team"]
            key = match_key(home, away)
            picks = {"h2h": None, "line": None, "total": None}
            for p in match.get("predictions", []):
                m = p.get("market")
                if m == "H2H" and picks["h2h"] is None:
                    picks["h2h"] = {
                        "pick": p["pick"], "odds": p["odds"],
                        "prob": p.get("model_probability"),
                    }
                elif m == "MBand" and picks["line"] is None:
                    picks["line"] = {"pick": p["pick"], "odds": p["odds"]}
                elif m == "Total" and picks["total"] is None:
                    picks["total"] = {
                        "pick": p["pick"], "odds": p["odds"],
                        "predicted_total": p.get("predicted_total"),
                    }
            out[key] = {
                "home_team": home, "away_team": away,
                "venue": match.get("venue"), "match_date": match.get("match_date"),
                "picks": picks,
            }
    return out


# ---------------------------------------------------------------------------
# Stats Insider: bulk endpoint returns every sport mixed together;
# filter to NRL client-side (the Sport= query param is ignored server-side)
# ---------------------------------------------------------------------------

def get_si_predictions():
    data = fetch_json(SI_URL)
    out = {}
    for entry in data:
        md = entry.get("MatchData", {})
        if md.get("Sport") != "NRL":
            continue
        home = md.get("HomeTeam", {}).get("DisplayName") or md.get("HomeTeam", {}).get("Nickname")
        away = md.get("AwayTeam", {}).get("DisplayName") or md.get("AwayTeam", {}).get("Nickname")
        if not home or not away:
            continue
        key = match_key(home, away)

        bet = entry.get("aggregatedBettingInfo", {})
        pre = entry.get("PreData", {})

        home_odds = bet.get("HomeOdds")
        away_odds = bet.get("AwayOdds")
        pythag_home = pre.get("PythagHome")
        h2h_pick, h2h_odds, h2h_prob = None, None, None
        if pythag_home is not None:
            if pythag_home >= 0.5:
                h2h_pick, h2h_odds, h2h_prob = home, home_odds, pythag_home
            else:
                h2h_pick, h2h_odds, h2h_prob = away, away_odds, pre.get("PythagAway")

        home_line = bet.get("HomeLine")
        line_pick, line_odds = None, None
        if home_line is not None:
            home_line_prob = bet.get("HomeLineWinPct")
            if home_line_prob is not None and home_line_prob >= 0.5:
                line_pick = f"{home} {'+' if home_line > 0 else ''}{home_line}"
                line_odds = bet.get("HomeLineOdds")
            else:
                away_line = -home_line
                line_pick = f"{away} {'+' if away_line > 0 else ''}{away_line}"
                line_odds = bet.get("AwayLineOdds")

        total_line = bet.get("TotalLine")
        over_prob = pre.get("OverWinPct")
        total_pick, total_odds = None, None
        if total_line is not None and over_prob is not None:
            if over_prob >= 0.5:
                total_pick, total_odds = "Over", bet.get("OverOdds")
            else:
                total_pick, total_odds = "Under", bet.get("UnderOdds")

        out[key] = {
            "home_team": home, "away_team": away,
            "venue": md.get("Venue"), "match_date": md.get("Date"),
            "picks": {
                "h2h": {"pick": h2h_pick, "odds": h2h_odds, "prob": h2h_prob} if h2h_pick else None,
                "line": {"pick": line_pick, "odds": line_odds} if line_pick else None,
                "total": {"pick": total_pick, "odds": total_odds, "line": total_line} if total_pick else None,
            },
        }
    return out


# ---------------------------------------------------------------------------
# Cross-match
# ---------------------------------------------------------------------------

def pick_side(pick_str, home, away):
    """Reduce a pick like 'Warriors +5.5' or 'Over' down to a comparable token."""
    if pick_str is None:
        return None
    p = pick_str.strip()
    if p.lower() in ("over", "under"):
        return p.capitalize()
    for team in (home, away):
        if p.lower().startswith(team.lower()):
            return team
    return p


def cross_match(alphr, si):
    all_keys = sorted(set(alphr) | set(si))
    matches = []
    for key in all_keys:
        a = alphr.get(key)
        s = si.get(key)
        home = (a or s)["home_team"]
        away = (a or s)["away_team"]
        venue = (a or {}).get("venue") or (s or {}).get("venue")
        match_date = (a or {}).get("match_date") or (s or {}).get("match_date")

        markets = {}
        for m in ("h2h", "line", "total"):
            ap = a["picks"][m] if a else None
            sp = s["picks"][m] if s else None
            a_side = pick_side(ap["pick"], home, away) if ap else None
            s_side = pick_side(sp["pick"], home, away) if sp else None
            agree = bool(a_side and s_side and a_side == s_side)
            markets[m] = {"alphr": ap, "si": sp, "agree": agree}

        matches.append({
            "home_team": home, "away_team": away, "venue": venue,
            "match_date": match_date, "markets": markets,
        })
    matches.sort(key=lambda m: m["match_date"] or "")
    return matches


def display_pick(mk_key, alphr_pick, si_pick, si_total_line=None):
    """Build a human-readable pick string with the actual number attached.
    Alphr's Line/Total markets return only a side ('Titans', 'Over') with
    no bookmaker number — the real number lives in Stats Insider's data,
    so use SI's line/total number since these are agreement-only entries."""
    if mk_key == "h2h":
        return alphr_pick
    if mk_key == "line" and si_pick:
        return si_pick  # e.g. "Titans +5.5"
    if mk_key == "total":
        if si_total_line is not None:
            return f"{alphr_pick} {si_total_line}"  # e.g. "Under 45.5"
        return alphr_pick
    return alphr_pick


def build_agreed_list(matches):
    agreed = []
    for m in matches:
        for label, key in (("H2H", "h2h"), ("Line", "line"), ("Total", "total")):
            mk = m["markets"][key]
            if mk["agree"]:
                si_total_line = mk["si"].get("line") if key == "total" else None
                pick_text = display_pick(key, mk["alphr"]["pick"], mk["si"]["pick"], si_total_line)
                agreed.append({
                    "match": f"{m['home_team']} v {m['away_team']}",
                    "market": label,
                    "pick": pick_text,
                    "alphr_odds": mk["alphr"]["odds"],
                    "si_odds": mk["si"]["odds"],
                    "match_date": m["match_date"],
                })
    return agreed


# ---------------------------------------------------------------------------
# HTML rendering — inline CSS/JS, no explainer text, two sections only
# ---------------------------------------------------------------------------

def fmt_odds(o):
    return f"${o:.2f}" if isinstance(o, (int, float)) else "—"


def esc(s):
    return "" if s is None else str(s).replace("&", "&amp;").replace("<", "&lt;")


def fmt_datetime_gmt6(iso_str):
    """Render an ISO datetime string in GMT+6."""
    if not iso_str:
        return ""
    try:
        d = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        d = d.astimezone(timezone(timedelta(hours=6)))
        return d.strftime("%a %d %b, %I:%M %p") + " GMT+6"
    except ValueError:
        return iso_str


def render_agreed_section(agreed):
    if not agreed:
        return '<p class="empty">No agreements this round.</p>'
    rows = []
    for item in agreed:
        when = fmt_datetime_gmt6(item.get("match_date"))
        rows.append(f'''
      <div class="agree-card">
        <div class="agree-match">{esc(item['match'])}<span class="agree-date">{when}</span></div>
        <div class="agree-line"><span class="market-tag">{esc(item['market'])}</span> {esc(item['pick'])}
          <span class="odds">({fmt_odds(item['alphr_odds'])} / {fmt_odds(item['si_odds'])})</span></div>
      </div>''')
    return "\n".join(rows)


def render_market_line(label, mk, mk_key):
    a = mk["alphr"]
    s = mk["si"]

    def side_text(entry, source):
        if not entry or not entry.get("pick"):
            return "—"
        pick = entry["pick"]
        # Alphr's Total/Line picks are bare side names with no number attached;
        # borrow SI's numeric line so both rows always show what's being bet.
        if mk_key == "total":
            line_val = entry.get("line") if source == "si" else (s.get("line") if s else None)
            if line_val is not None and not any(c.isdigit() for c in pick):
                pick = f"{pick} {line_val}"
        elif mk_key == "line" and source == "alphr":
            # Alphr gives only the team name; SI's pick has the number for the same team
            if s and s.get("pick") and pick.lower() in s["pick"].lower():
                pick = s["pick"]
        return f"{esc(pick)} ({fmt_odds(entry['odds'])})"

    a_txt = side_text(a, "alphr")
    s_txt = side_text(s, "si")
    cls = "agree" if mk["agree"] else "disagree"
    return f'<div class="mkt-row {cls}"><span class="mkt-label">{label}</span><span class="mkt-si">SI: {s_txt}</span><span class="mkt-a">Alphr: {a_txt}</span></div>'


def render_match_block(m):
    dt = fmt_datetime_gmt6(m["match_date"])
    lines = "\n".join(
        render_market_line(label, m["markets"][key], key)
        for label, key in (("H2H", "h2h"), ("Line", "line"), ("Total", "total"))
    )
    venue = f" · {esc(m['venue'])}" if m["venue"] else ""
    return f'''
      <div class="match-block">
        <div class="match-head">{esc(m['home_team'])} v {esc(m['away_team'])}<span class="match-meta">{dt}{venue}</span></div>
        {lines}
      </div>'''


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NRL Signal</title>
<style>
:root {{
  --bg: #FAF7F2; --ink: #1A1A1A; --card: #ffffff; --border: #E5DFD3;
  --accent: #0F5132; --muted: #7A756B; --disagree: #B8860B;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #12151C; --ink: #EDEBE6; --card: #1A1E27; --border: #2A2F3A;
    --accent: #2ECC71; --muted: #9A968D; --disagree: #E0A526;
  }}
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 16px; background: var(--bg); color: var(--ink);
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  max-width: 720px; margin-left: auto; margin-right: auto;
}}
h1 {{ font-size: 1.3rem; margin: 4px 0 16px; }}
h2 {{ font-size: 1rem; margin: 24px 0 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }}
.agree-card {{
  background: var(--card); border: 1px solid var(--border); border-radius: 16px;
  padding: 12px 16px; margin-bottom: 10px;
}}
.agree-match {{ font-weight: 600; margin-bottom: 2px; }}
.agree-date {{ display: block; font-weight: 400; font-size: 0.78rem; color: var(--muted); margin: 2px 0 6px; }}
.agree-line {{ font-size: 0.95rem; }}
.market-tag {{
  display: inline-block; background: var(--accent); color: #fff;
  font-size: 0.75rem; font-weight: 600; padding: 2px 8px; border-radius: 8px; margin-right: 6px;
}}
.odds {{ color: var(--muted); font-variant-numeric: tabular-nums; }}
.match-block {{
  background: var(--card); border: 1px solid var(--border); border-radius: 16px;
  padding: 12px 16px; margin-bottom: 10px;
}}
.match-head {{ font-weight: 600; margin-bottom: 8px; }}
.match-meta {{ display: block; font-weight: 400; font-size: 0.8rem; color: var(--muted); margin-top: 2px; }}
.mkt-row {{
  display: flex; flex-wrap: wrap; gap: 6px 10px; font-size: 0.9rem; padding: 4px 0;
  border-top: 1px solid var(--border);
}}
.mkt-row:first-of-type {{ border-top: none; }}
.mkt-label {{ font-weight: 600; width: 42px; flex-shrink: 0; }}
.mkt-row.agree .mkt-label {{ color: var(--accent); }}
.mkt-row.disagree .mkt-label {{ color: var(--disagree); }}
.mkt-si, .mkt-a {{ font-variant-numeric: tabular-nums; }}
.empty {{ color: var(--muted); }}
.updated {{ color: var(--muted); font-size: 0.8rem; margin-bottom: 20px; }}
</style>
</head>
<body>
<h1>NRL Signal</h1>
<div class="updated">Updated {generated_at}</div>

<h2>Agreed Picks</h2>
{agreed_html}

<h2>All Matches</h2>
{matches_html}

</body>
</html>
"""


def render_html(matches, agreed):
    generated_at = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    agreed_html = render_agreed_section(agreed)
    matches_html = "\n".join(render_match_block(m) for m in matches)
    return HTML_TEMPLATE.format(
        generated_at=generated_at, agreed_html=agreed_html, matches_html=matches_html,
    )


def main():
    print("Fetching Alphr predictions...")
    alphr = get_alphr_predictions()
    print(f"  {len(alphr)} NRL matches from Alphr")

    print("Fetching Stats Insider predictions...")
    si = get_si_predictions()
    print(f"  {len(si)} NRL matches from Stats Insider")

    matches = cross_match(alphr, si)
    agreed = build_agreed_list(matches)
    print(f"Cross-matched {len(matches)} matches, {len(agreed)} agreed picks")

    html = render_html(matches, agreed)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Wrote docs/index.html")


if __name__ == "__main__":
    main()
