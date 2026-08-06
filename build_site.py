# -*- coding: utf-8 -*-
"""
build_site.py v2 — Obsidian の日次ブリーフ + 週次インテリジェンスから GitHub Pages サイトを全量再生成する。

  ソース: 02_NOTES/news-briefs/YYYY-MM-DD_daily-ai-news-brief.md
          02_NOTES/weeklies/YYYY-WXX-Intelligence.md
  出力:   index.html / archive.html / search.html / search-index.json
          brief-YYYY-MM-DD.html / weekly-YYYY-WXX.html / robots.txt
  対象:   SITE_START (2026-07-01) 以降
  除外:   「💬 私のメモ」「💬 マイメモ」「💬 論点・ネタ帳」(個人入力欄は公開しない)

使い方:
  python build_site.py                 # リポジトリ直下に生成
  python build_site.py --out DIR       # 別ディレクトリに生成 (プレビュー用)
  python build_site.py --publish       # 生成 + git add/commit/push
"""
import re
import sys
import json
import subprocess
import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

VAULT = Path(r"C:\Users\yasu\Obsidian Vault desktop\02_NOTES")
VAULT_BRIEFS = VAULT / "news-briefs"
VAULT_WEEKLIES = VAULT / "weeklies"
REPO = Path(__file__).resolve().parent
SITE_START = "2026-07-01"
SITE_TITLE = "Daily AI News Brief"
SITE_SUB = "医薬品開発 × CRO"
SIDEBAR_RECENT = 14

WD = ["月", "火", "水", "木", "金", "土", "日"]

# ---------------------------------------------------------------- helpers


def esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def inline(t: str) -> str:
    """インライン markdown (リンク・太字) を HTML に。先に HTML エスケープ。"""
    t = esc(t)
    t = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        r'<a href="\2" target="_blank" rel="noopener">\1</a>',
        t,
    )
    t = re.sub(r"(?<!\w)(https?://[^\s<>「」（）]+)", r'<a href="\1" target="_blank" rel="noopener">\1</a>', t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    return t


def plain(t: str) -> str:
    """検索インデックス・抜粋用にマークアップを落とす。"""
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+", "", t)
    return t.replace("**", "").replace("*", "").replace("|", " ").strip()


def clip(t: str, n: int) -> str:
    t = t.strip()
    return t if len(t) <= n else t[:n] + "…"


def weekday_of(date: str) -> str:
    y, m, d = (int(x) for x in date.split("-"))
    return WD[datetime.date(y, m, d).weekday()]


def strip_private(text: str) -> str:
    """💬 で始まる引用ブロック(個人メモ欄)を除去する。"""
    out, skip = [], False
    for line in text.splitlines():
        s = line.strip()
        if re.match(r"^>\s*(\*\*)?\s*💬", s):
            skip = True
            continue
        if skip:
            if s == "" or s.startswith(">"):
                continue
            skip = False
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------- 共通ブロックパーサ


def parse_blocks(text: str):
    """本文を ('p'|'ul'|'table'|'quote'|'note', payload) の列に分解する。"""
    out, tbl, ul, q = [], [], [], []

    def flush():
        nonlocal tbl, ul, q
        if tbl:
            out.append(("table", tbl))
            tbl = []
        if ul:
            out.append(("ul", ul))
            ul = []
        if q:
            out.append(("quote", " ".join(q)))
            q = []

    for raw in text.splitlines():
        s = raw.strip()
        if not s or s == "---":
            flush()
            continue
        if s.startswith("|"):
            if ul or q:
                flush()
            tbl.append(s)
            continue
        if s.startswith(">"):
            if tbl or ul:
                flush()
            q.append(s.lstrip(">").strip())
            continue
        if s.startswith("- ") or s.startswith("* "):
            if tbl or q:
                flush()
            ul.append(s[2:].strip())
            continue
        flush()
        if s.startswith("*") and not s.startswith("**") and s.endswith("*"):
            out.append(("note", s.strip("*").strip()))
        else:
            out.append(("p", s))
    flush()
    return out


def blocks_text(bl) -> str:
    """検索用にブロック列をプレーンテキスト化。"""
    buf = []
    for kind, payload in bl:
        if kind == "table":
            buf.extend(plain(r) for r in payload)
        elif kind == "ul":
            buf.extend(plain(i) for i in payload)
        else:
            buf.append(plain(payload))
    return " ".join(x for x in buf if x)


# ---------------------------------------------------------------- 日次パーサ


def split_sections(text: str) -> dict:
    parts = re.split(r"^## ", text, flags=re.M)
    secs = {}
    for p in parts[1:]:
        header, _, body = p.partition("\n")
        secs[header.strip()] = body
    return secs


def section_by_emoji(secs: dict, emoji: str) -> str:
    for k, v in secs.items():
        if emoji in k:
            return v
    return ""


def parse_takeaways(body: str):
    rows = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        if re.match(r"^\|[\s\-:|]+\|$", line):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or cells[0] in ("テーマ", ""):
            continue
        if len(cells) >= 3:
            rows.append((cells[0], " | ".join(cells[1:-1]), cells[-1]))
    return rows


def parse_articles(body: str):
    arts, cur = [], None
    for line in body.splitlines():
        if line.startswith("- "):
            if cur:
                arts.append(cur)
            m = re.match(r"- \*\*\[(.+?)\]\((\S+?)\)\*\*\s*(.*)$", line)
            m2 = re.match(r"- \*\*(.+?)\*\*\s*(.*)$", line) if not m else None
            if m:
                meta = re.sub(r"^[—\-–\s]+", "", m.group(3).strip())
                cur = {"title": m.group(1), "url": m.group(2), "meta": meta, "body": []}
            elif m2:
                meta = re.sub(r"^[—\-–\s]+", "", m2.group(2).strip())
                cur = {"title": m2.group(1), "url": None, "meta": meta, "body": []}
            else:
                cur = {"title": None, "url": None, "meta": "", "body": [line[2:].strip()]}
        elif cur is not None and line.strip() and not line.startswith("#"):
            cur["body"].append(line.strip())
    if cur:
        arts.append(cur)
    return arts


def parse_overseas(body: str):
    groups = []
    for p in re.split(r"^### ", body, flags=re.M)[1:]:
        header, _, sub = p.partition("\n")
        groups.append((header.strip(), parse_articles(sub)))
    return groups


def parse_impact(body: str):
    groups = []
    for p in re.split(r"^### ", body, flags=re.M)[1:]:
        header, _, sub = p.partition("\n")
        paras = [
            l.strip()
            for l in sub.splitlines()
            if l.strip() and not l.strip().startswith("---") and not l.strip().startswith("*本ブリーフ")
        ]
        groups.append((header.strip(), paras))
    return groups


def parse_x_section(body: str):
    items, notice_buf, arts_body = [], [], []
    for line in body.splitlines():
        s = line.rstrip()
        if s.strip().startswith(">"):
            notice_buf.append(s.strip().lstrip(">").strip())
            continue
        if notice_buf:
            items.append(("notice", " ".join(notice_buf)))
            notice_buf = []
        if s.startswith("- ") or (s.startswith("  ") and s.strip()):
            arts_body.append(s)
            continue
        if arts_body and not s.strip():
            arts_body.append(s)
            continue
        if s.strip() and not s.strip().startswith("---"):
            items.append(("plain", s.strip()))
    if notice_buf:
        items.append(("notice", " ".join(notice_buf)))
    for art in parse_articles("\n".join(arts_body)):
        items.append(("post", art))
    return items


def domestic_note(body: str) -> str:
    """国内欄が「新着なし」等の地の文だけの日は、その一行を拾って表示する。"""
    for line in body.splitlines():
        s = line.strip()
        if s and not s.startswith(("-", "#", ">", "|", "---")):
            return s
    return ""


def parse_brief(path: Path) -> dict:
    text = strip_private(path.read_text(encoding="utf-8"))
    date = path.name[:10]
    m = re.search(r"^# .*?\|\s*(.+?)\s*$", text, re.M)
    date_label = m.group(1) if m else date
    secs = split_sections(text)
    fm = re.findall(r"^\*([^*].*?)\*\s*$", text, re.M)
    return {
        "kind": "daily",
        "date": date,
        "date_label": date_label,
        "slug": f"brief-{date}",
        "takeaways": parse_takeaways(section_by_emoji(secs, "📌")),
        "overseas": parse_overseas(section_by_emoji(secs, "🌍")),
        "domestic": parse_articles(section_by_emoji(secs, "🇯🇵")),
        "domestic_note": domestic_note(section_by_emoji(secs, "🇯🇵")),
        "x_items": parse_x_section(section_by_emoji(secs, "📱")),
        "impact": parse_impact(section_by_emoji(secs, "📊")),
        "footer": fm[-1] if fm else "",
    }


def brief_article_count(b: dict) -> int:
    return sum(len(a) for _, a in b["overseas"]) + len(b["domestic"])


# ---------------------------------------------------------------- 週次パーサ


def parse_weekly(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    text = strip_private(raw)
    meta = {}
    fm = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if fm:
        for line in fm.group(1).splitlines():
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
        text = text[fm.end():]
    week = meta.get("week") or path.name[:8]
    period = meta.get("period", "")
    sections = []
    for p in re.split(r"^## ", text, flags=re.M)[1:]:
        header, _, body = p.partition("\n")
        h = header.strip()
        if "💬" in h:
            continue
        items = []
        parts = re.split(r"^### ", body, flags=re.M)
        if parts[0].strip():
            items.append(("intro", parse_blocks(parts[0])))
        for q in parts[1:]:
            ah, _, sub = q.partition("\n")
            t = ah.strip().lstrip("📌").strip()
            m = re.match(r"^\[(.+?)\]\((\S+?)\)\s*$", t)
            title, url = (m.group(1), m.group(2)) if m else (t, None)
            items.append(("article", {"title": title, "url": url, "blocks": parse_blocks(sub)}))
        sections.append((h, items))
    # 期間の終端日 (掲載可否の判定に使う)
    pm = re.findall(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", period)
    end = ""
    if pm:
        y, mo, d = pm[-1]
        if len(pm) > 1 and int(pm[-1][1]) < int(pm[0][1]):
            y = str(int(pm[0][0]))
        end = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    summary = []
    for h, items in sections:
        if "エグゼクティブサマリー" in h:
            for kind, payload in items:
                if kind == "intro":
                    for bk, bp in payload:
                        if bk == "ul":
                            summary.extend(bp)
    return {
        "kind": "weekly",
        "week": week,
        "period": period,
        "date": meta.get("created") or meta.get("date") or end,
        "end": end or meta.get("date", ""),
        "slug": f"weekly-{week}",
        "sections": sections,
        "summary": summary,
    }


# ---------------------------------------------------------------- CSS / JS

LIGHT = """
  --bg:#f6f7fb; --bg-card:#ffffff; --bg-sidebar:#ffffff; --bg-soft:#f0f2f8;
  --border:#e1e4ee; --border-soft:#edeff6;
  --text:#171b26; --text-mid:#3d4456; --text-dim:#646d84;
  --accent:#4553c9; --accent-text:#3a47bb; --accent-soft:rgba(69,83,201,.09);
  --amber:#9a6b0d; --amber-soft:rgba(200,150,20,.10); --amber-line:rgba(200,150,20,.35);
  --teal:#0d7d75; --shadow:0 1px 2px rgba(20,25,45,.06), 0 4px 14px rgba(20,25,45,.05);
"""

DARK = """
  --bg:#0e1016; --bg-card:#181b28; --bg-sidebar:#12141e; --bg-soft:#1f2331;
  --border:#2a2e40; --border-soft:#222635;
  --text:#e7e9f3; --text-mid:#c3c8d8; --text-dim:#8f95ab;
  --accent:#7c8cff; --accent-text:#9aa6ff; --accent-soft:rgba(124,140,255,.13);
  --amber:#f0c濁; --amber-soft:rgba(240,196,80,.09); --amber-line:rgba(240,196,80,.30);
  --teal:#57d3ca; --shadow:none;
"""

DARK = DARK.replace("#f0c濁", "#f0c450")

CSS = """
*{margin:0;padding:0;box-sizing:border-box}
:root{__LIGHT__}
@media (prefers-color-scheme:dark){:root{__DARK__}}
:root[data-theme="dark"]{__DARK__}
:root[data-theme="light"]{__LIGHT__}
/* overflow-x は clip。hidden にすると html/body がスクロールコンテナ化して topbar の sticky が壊れる */
html,body{overflow-x:clip;max-width:100%}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Hiragino Sans","Hiragino Kaku Gothic ProN","Yu Gothic UI","Noto Sans JP",Meiryo,sans-serif;
  background:var(--bg);color:var(--text);line-height:1.85;font-size:15px;
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
a{color:var(--accent-text)}
img{max-width:100%}

/* ---- layout ---- */
.sidebar{position:fixed;top:0;left:0;width:252px;height:100vh;overflow-y:auto;overscroll-behavior:contain;
  background:var(--bg-sidebar);border-right:1px solid var(--border);z-index:30}
.content{margin-left:252px;min-width:0}
.main{padding:30px 36px 60px;min-width:0}
.wrap{max-width:880px;margin:0 auto;min-width:0}
.wrap-wide{max-width:1120px;margin:0 auto;min-width:0}

/* ---- topbar (検索は常時表示) ---- */
.topbar{position:sticky;top:0;z-index:15;background:var(--bg);border-bottom:1px solid var(--border);
  padding:10px 36px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.gwrap{position:relative;flex:1 1 240px;min-width:0;max-width:560px}
.gwrap::before{content:"🔎";position:absolute;left:11px;top:50%;transform:translateY(-50%);font-size:13px;opacity:.6;pointer-events:none}
#gq{width:100%;font:inherit;font-size:14px;padding:8px 13px 8px 33px;border-radius:10px;
  border:1px solid var(--border);background:var(--bg-card);color:var(--text)}
#gq:focus{outline:2px solid var(--accent);outline-offset:1px}
.sres{display:none;position:absolute;top:calc(100% + 6px);left:0;right:0;background:var(--bg-card);
  border:1px solid var(--border);border-radius:12px;box-shadow:0 10px 34px rgba(10,14,30,.20);
  max-height:62vh;overflow-y:auto;z-index:40}
.sres.on{display:block}
.sr{display:block;padding:10px 14px;text-decoration:none;border-bottom:1px solid var(--border-soft)}
.sr:last-child{border-bottom:none}
.sr:hover{background:var(--accent-soft)}
.sr-h{font-size:12.5px;font-weight:700;color:var(--accent-text)}
.sr-s{font-size:12.5px;color:var(--text-mid);margin-top:3px;line-height:1.7}
.sr-all{display:block;padding:9px 14px;text-align:center;font-size:12.5px;font-weight:700;
  color:var(--accent-text);text-decoration:none;background:var(--bg-soft)}
.tb-sp{flex:1}

/* ---- sidebar ---- */
.logo{padding:20px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px}
.logo-icon{font-size:22px}
.logo-text{font-size:15px;font-weight:700;letter-spacing:.01em}
.logo-sub{font-size:11px;color:var(--text-dim);font-weight:500;letter-spacing:.04em}
.nav{padding:10px 8px 40px}
.nav-title{padding:16px 12px 6px;font-size:10.5px;letter-spacing:.13em;text-transform:uppercase;color:var(--text-dim);font-weight:700}
.nav-item{display:flex;align-items:center;gap:9px;padding:7px 12px;border-radius:8px;color:var(--text-dim);text-decoration:none;font-size:13.5px;line-height:1.4}
.nav-item:hover{background:var(--accent-soft);color:var(--text)}
.nav-item.active{background:var(--accent-soft);color:var(--accent-text);font-weight:700}
.nav-item .wd{font-size:11px;opacity:.7}
.nav-item .dot{width:5px;height:5px;border-radius:50%;background:var(--border);flex:none}
.nav-item.active .dot{background:var(--accent)}
details.nav-more{margin-top:2px}
details.nav-more>summary{list-style:none;cursor:pointer;padding:7px 12px;border-radius:8px;color:var(--text-dim);font-size:13px}
details.nav-more>summary::-webkit-details-marker{display:none}
details.nav-more>summary:hover{background:var(--accent-soft);color:var(--text)}
details.nav-more>summary::before{content:"▸ ";font-size:10px}
details.nav-more[open]>summary::before{content:"▾ "}

/* ---- header ---- */
.page-head{margin-bottom:26px}
.eyebrow{font-size:11.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--text-dim);font-weight:700}
.page-head h1{font-size:27px;font-weight:800;line-height:1.35;letter-spacing:-.01em;margin-top:4px}
.page-head .sub{color:var(--text-dim);font-size:13.5px;margin-top:6px}

/* ---- cards ---- */
.card{background:var(--bg-card);border:1px solid var(--border);border-radius:14px;padding:20px 22px;margin-bottom:18px;box-shadow:var(--shadow);max-width:100%;overflow-wrap:anywhere}
.card>h2{font-size:16px;font-weight:700;letter-spacing:.01em;margin-bottom:14px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.hero{border-left:4px solid var(--accent)}
.sub-h{font-size:11.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--text-dim);font-weight:700;margin:20px 0 6px;padding-bottom:5px;border-bottom:1px solid var(--border-soft)}
.sub-h:first-of-type{margin-top:4px}

/* ---- takeaway ---- */
.tw{display:flex;gap:12px;align-items:flex-start;padding:12px 0;border-bottom:1px solid var(--border-soft);flex-wrap:wrap}
.tw:last-child{border-bottom:none;padding-bottom:0}
.tw-chip{flex:none;font-size:11px;padding:2px 9px;border-radius:20px;background:var(--accent-soft);color:var(--accent-text);font-weight:700;white-space:nowrap;margin-top:5px}
.tw-body{flex:1 1 260px;min-width:0}
.tw-text{font-size:14.5px;line-height:1.8}
.tw-src{font-size:12px;margin-top:3px}
.tw-src a{color:var(--teal);text-decoration:none}
.tw-src a:hover{text-decoration:underline}

/* ---- article ---- */
.art{border:1px solid var(--border-soft);border-radius:11px;padding:15px 17px;margin:11px 0;background:var(--bg-card)}
.art:hover{border-color:var(--border)}
.art h3{font-size:15.5px;font-weight:700;line-height:1.6;letter-spacing:-.005em}
.art h3 a{color:var(--text);text-decoration:none}
.art h3 a:hover{color:var(--accent-text)}
.art-meta{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center;font-size:11.5px;color:var(--text-dim);margin:7px 0 9px}
.src{background:var(--accent-soft);color:var(--accent-text);border-radius:20px;padding:2px 9px;font-weight:700}
.art-body{font-size:14px;color:var(--text-mid);line-height:1.85}
.art-body p+p{margin-top:8px}

/* ---- misc blocks ---- */
.quote{border-left:3px solid var(--amber-line);padding:10px 14px;margin:11px 0;background:var(--amber-soft);border-radius:0 8px 8px 0;font-size:13.5px;color:var(--text-mid)}
.quote .q-name{font-weight:700;color:var(--amber);font-size:14px}
.quote .q-name a{color:var(--amber)}
.notice{font-size:13px;color:var(--text-mid);background:var(--bg-soft);border:1px solid var(--border-soft);border-radius:9px;padding:10px 14px;margin:11px 0}
.x-plain{font-size:13px;color:var(--text-dim);margin:8px 0}
.impact-h{font-size:14.5px;font-weight:700;color:var(--accent-text);margin:16px 0 6px}
.impact-h:first-of-type{margin-top:2px}
.impact-p{font-size:14px;color:var(--text-mid)}
.impact-p+.impact-p{margin-top:8px}
ul.bl{margin:6px 0 6px 2px;list-style:none}
ul.bl li{position:relative;padding-left:16px;font-size:14px;color:var(--text-mid);margin:6px 0}
ul.bl li::before{content:"";position:absolute;left:2px;top:.72em;width:5px;height:5px;border-radius:50%;background:var(--accent);opacity:.55}
.note-line{font-size:12.5px;color:var(--text-dim);margin:10px 0}

/* ---- table → cards (横スクロールを作らない) ---- */
.tcards{display:grid;gap:10px;margin:10px 0}
.trow{border:1px solid var(--border-soft);border-radius:11px;padding:13px 15px;background:var(--bg-card)}
.tr-h{font-size:14.5px;font-weight:700;line-height:1.6;display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.tr-date{font-size:11px;font-weight:700;color:var(--text-dim);background:var(--bg-soft);border-radius:20px;padding:1px 8px;flex:none}
.tr-f{display:flex;gap:10px;margin-top:7px;flex-wrap:wrap;font-size:13.5px}
.tr-k{flex:none;width:88px;color:var(--text-dim);font-size:11.5px;font-weight:700;padding-top:3px;letter-spacing:.03em}
.tr-v{flex:1 1 220px;min-width:0;color:var(--text-mid);line-height:1.75}

/* ---- archive ---- */
.mo-h{font-size:13px;font-weight:800;letter-spacing:.06em;color:var(--text-dim);margin:26px 0 8px;padding-bottom:6px;border-bottom:1px solid var(--border)}
.mo-h:first-child{margin-top:0}
.row{display:flex;gap:12px;align-items:baseline;padding:9px 4px;border-bottom:1px solid var(--border-soft);text-decoration:none;flex-wrap:wrap}
.row:hover{background:var(--accent-soft);border-radius:8px}
.row-d{flex:none;font-size:13px;font-weight:700;color:var(--accent-text);width:92px}
.row-w{flex:none;font-size:11px;color:var(--text-dim);width:52px}
.row-t{flex:1 1 240px;min-width:0;font-size:13px;color:var(--text-mid)}
.row.wk .row-d{color:var(--teal)}

/* ---- prev/next ---- */
.pager{display:flex;gap:10px;justify-content:space-between;margin:22px 0 0;flex-wrap:wrap}
.pg{flex:1 1 200px;border:1px solid var(--border);border-radius:11px;padding:11px 14px;text-decoration:none;background:var(--bg-card);min-width:0}
.pg:hover{border-color:var(--accent)}
.pg span{display:block;font-size:11px;color:var(--text-dim)}
.pg b{font-size:13.5px;color:var(--accent-text)}
.pg.right{text-align:right}

/* ---- search ---- */
.sbox{display:flex;gap:10px;flex-wrap:wrap}
#q{flex:1 1 240px;min-width:0;font:inherit;font-size:15px;padding:11px 14px;border-radius:11px;border:1px solid var(--border);background:var(--bg-card);color:var(--text)}
#q:focus{outline:2px solid var(--accent);outline-offset:1px}
.chips{display:flex;gap:7px;flex-wrap:wrap;margin-top:12px}
.chip{font-size:12px;padding:4px 11px;border-radius:20px;border:1px solid var(--border);background:var(--bg-card);color:var(--text-dim);cursor:pointer;font-family:inherit}
.chip:hover,.chip.on{background:var(--accent-soft);color:var(--accent-text);border-color:var(--accent)}
.hits{font-size:12.5px;color:var(--text-dim);margin:16px 0 8px}
.hit{display:block;text-decoration:none;border:1px solid var(--border-soft);border-radius:11px;padding:13px 15px;margin:9px 0;background:var(--bg-card)}
.hit:hover{border-color:var(--accent)}
.hit-h{font-size:13.5px;font-weight:700;color:var(--accent-text)}
.hit-s{font-size:13px;color:var(--text-mid);margin-top:5px;line-height:1.8}
mark{background:var(--amber-soft);color:var(--amber);padding:0 2px;border-radius:3px;font-weight:700}

.foot{color:var(--text-dim);font-size:12px;margin-top:30px;border-top:1px solid var(--border);padding-top:14px}

/* ---- theme toggle ---- */
.tt{background:none;border:1px solid var(--border);border-radius:8px;color:var(--text-dim);font:inherit;font-size:13px;padding:5px 10px;cursor:pointer}
.tt:hover{color:var(--text);border-color:var(--accent)}

.mhead{display:none}
.ovl{display:none}
@media (max-width:900px){
  .mhead{display:flex;align-items:center;gap:10px;position:fixed;top:0;left:0;right:0;z-index:35;height:46px;
    background:var(--bg-sidebar);border-bottom:1px solid var(--border);padding:0 13px}
  .mhead .logo-text{font-size:14px}
  .mhead .sp{flex:1}
  .burger{background:none;border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:17px;padding:3px 10px;cursor:pointer}
  .sidebar{left:-270px;width:258px;transition:left .2s}
  .sidebar.open{left:0}
  .ovl{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:25}
  .ovl.active{display:block}
  .content{margin-left:0;padding-top:46px}
  .topbar{top:46px;padding:8px 13px}
  .topbar .tt{display:none}
  .gwrap{max-width:none}
  .main{padding:16px 15px 50px}
  .page-head h1{font-size:22px}
  .card{padding:16px 15px;border-radius:12px}
  .tr-k{width:100%;padding-top:0}
}
"""

CSS = CSS.replace("__LIGHT__", LIGHT).replace("__DARK__", DARK)

THEME_BOOT = """
(function(){var t=localStorage.getItem('nb-theme');if(t)document.documentElement.dataset.theme=t;})();
"""

JS = """
var mb=document.getElementById('menuBtn'),sb=document.getElementById('sidebar'),ov=document.getElementById('ovl');
if(mb){mb.addEventListener('click',function(){sb.classList.toggle('open');ov.classList.toggle('active');});
ov.addEventListener('click',function(){sb.classList.remove('open');ov.classList.remove('active');});}
function tw(){var r=document.documentElement;var cur=r.dataset.theme||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light');
var nx=cur==='dark'?'light':'dark';r.dataset.theme=nx;localStorage.setItem('nb-theme',nx);
document.querySelectorAll('.tt').forEach(function(b){b.textContent=nx==='dark'?'☀ ライト':'☾ ダーク';});}
document.querySelectorAll('.tt').forEach(function(b){b.addEventListener('click',tw);
var d=document.documentElement.dataset.theme||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light');
b.textContent=d==='dark'?'☀ ライト':'☾ ダーク';});

/* ---- topbar 検索 (index は初回フォーカス時に遅延ロード) ---- */
var GI=null,GP=null,gq=document.getElementById('gq'),gr=document.getElementById('gres');
function gload(){if(!GP)GP=fetch('search-index.json').then(function(r){return r.json()}).then(function(j){GI=j});return GP}
function ge(s){return s.replace(/[&<>]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c]})}
function gsnip(t,q){var i=t.toLowerCase().indexOf(q.toLowerCase());if(i<0)return '';
var s=Math.max(0,i-30),e=Math.min(t.length,i+q.length+70);
return (s>0?'…':'')+ge(t.slice(s,i))+'<mark>'+ge(t.slice(i,i+q.length))+'</mark>'+ge(t.slice(i+q.length,e))+(e<t.length?'…':'');}
function grun(){if(!gq)return;var q=gq.value.trim();
if(!q||!GI){gr.classList.remove('on');gr.innerHTML='';return;}
var lq=q.toLowerCase(),hits=[];
GI.forEach(function(d){for(var i=0;i<d.x.length;i++){if(d.x[i].toLowerCase().indexOf(lq)>=0){hits.push({d:d,l:d.x[i]});return;}}});
if(!hits.length){gr.innerHTML='<div class="sr"><div class="sr-s">該当なし</div></div>';gr.classList.add('on');return;}
gr.innerHTML=hits.slice(0,7).map(function(h){
return '<a class="sr" href="'+h.d.u+'"><div class="sr-h">'+ge(h.d.t)+'</div><div class="sr-s">'+gsnip(h.l,q)+'</div></a>'}).join('')
+'<a class="sr-all" href="search.html?q='+encodeURIComponent(q)+'">'+hits.length+' 件すべての結果を見る →</a>';
gr.classList.add('on');}
if(gq){gq.addEventListener('focus',function(){gload().then(grun)});
gq.addEventListener('input',function(){gload().then(grun)});
gq.addEventListener('keydown',function(e){
if(e.key==='Enter'&&gq.value.trim()){location.href='search.html?q='+encodeURIComponent(gq.value.trim());}
if(e.key==='Escape'){gr.classList.remove('on');gq.blur();}});
document.addEventListener('click',function(e){if(!e.target.closest('.gwrap'))gr.classList.remove('on')});
document.addEventListener('keydown',function(e){
if((e.key==='/'||((e.ctrlKey||e.metaKey)&&e.key==='k'))&&document.activeElement!==gq){e.preventDefault();gq.focus();}});}
"""


# ---------------------------------------------------------------- shell


def render_sidebar(dailies, weeklies, active: str) -> str:
    def d_item(b):
        cls = " active" if b["slug"] == active else ""
        return (
            f'      <a href="{b["slug"]}.html" class="nav-item{cls}"><span class="dot"></span>'
            f'{b["date"][5:].replace("-", "/")}<span class="wd">{weekday_of(b["date"])}</span></a>'
        )

    def w_item(w):
        cls = " active" if w["slug"] == active else ""
        return f'      <a href="{w["slug"]}.html" class="nav-item{cls}"><span class="dot"></span>{esc(w["week"])}</a>'

    recent = [d_item(b) for b in dailies[:SIDEBAR_RECENT]]
    older = dailies[SIDEBAR_RECENT:]
    older_html = ""
    if older:
        by_month = {}
        for b in older:
            by_month.setdefault(b["date"][:7], []).append(b)
        blocks = []
        for mo, items in by_month.items():
            y, m = mo.split("-")
            rows = "\n".join(d_item(b) for b in items)
            blocks.append(
                f'      <details class="nav-more"><summary>{y}年{int(m)}月 ({len(items)})</summary>\n{rows}\n      </details>'
            )
        older_html = "\n" + "\n".join(blocks)

    wk_recent = [w_item(w) for w in weeklies[:8]]
    wk_older = weeklies[8:]
    wk_older_html = ""
    if wk_older:
        rows = "\n".join(w_item(w) for w in wk_older)
        wk_older_html = f'\n      <details class="nav-more"><summary>過去の週次 ({len(wk_older)})</summary>\n{rows}\n      </details>'

    def nav(href, icon, label, key):
        cls = " active" if active == key else ""
        return f'      <a href="{href}" class="nav-item{cls}"><span>{icon}</span>{label}</a>'

    wk_block = ""
    if weeklies:
        wk_block = (
            '      <div class="nav-title">週次インテリジェンス</div>\n'
            + "\n".join(wk_recent)
            + wk_older_html
            + "\n"
        )

    return f"""<div class="mhead">
  <button class="burger" id="menuBtn" aria-label="メニューを開く">☰</button>
  <div class="logo-icon">🧠</div>
  <div class="logo-text">AI Intel Brief</div>
  <div class="sp"></div>
  <button class="tt" type="button"></button>
</div>
<div class="ovl" id="ovl"></div>
<div class="layout">
  <aside class="sidebar" id="sidebar" aria-label="サイドメニュー">
    <div class="logo">
      <div class="logo-icon">🧠</div>
      <div>
        <div class="logo-text">AI Intel Brief</div>
        <div class="logo-sub">{esc(SITE_SUB)}</div>
      </div>
    </div>
    <nav class="nav">
{nav("index.html", "🏠", "最新ブリーフ", "index")}
{nav("search.html", "🔎", "検索", "search")}
{nav("archive.html", "🗂", "アーカイブ", "archive")}
      <div class="nav-title">デイリーブリーフ</div>
{chr(10).join(recent)}{older_html}
{wk_block}    </nav>
  </aside>"""


TOPBAR = """  <header class="topbar">
    <div class="gwrap">
      <input id="gq" type="search" autocomplete="off" placeholder="ブリーフを検索（例: ICON、PMDA、book-to-bill）">
      <div class="sres" id="gres"></div>
    </div>
    <div class="tb-sp"></div>
    <button class="tt" type="button"></button>
  </header>
"""

TOPBAR_PLAIN = """  <header class="topbar">
    <div class="tb-sp"></div>
    <button class="tt" type="button"></button>
  </header>
"""


def shell(title: str, sidebar: str, main_html: str, extra_js: str = "", wide=False, topsearch=True) -> str:
    cls = "wrap-wide" if wide else "wrap"
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<meta name="color-scheme" content="light dark">
<title>{esc(title)}</title>
<script>{THEME_BOOT}</script>
<style>{CSS}</style>
</head>
<body>
{sidebar}
  <div class="content">
{TOPBAR if topsearch else TOPBAR_PLAIN}    <main class="main"><div class="{cls}">
{main_html}
    </div></main>
  </div>
</div>
<script>{JS}{extra_js}</script>
</body>
</html>
"""


# ---------------------------------------------------------------- 共通レンダラ


def render_table(rows) -> str:
    cells = [[c.strip() for c in r.strip("|").split("|")] for r in rows]
    cells = [c for c in cells if not re.match(r"^[\s\-:|]*$", "".join(c))]
    if not cells:
        return ""
    head, body = cells[0], cells[1:]
    if not body:
        return ""
    date_first = head and head[0].strip() in ("日付", "Date", "date", "日時")
    out = []
    for row in body:
        if not row:
            continue
        if date_first and len(row) > 1:
            chip = f'<span class="tr-date">{esc(row[0])}</span>'
            title = inline(row[1])
            start = 2
        else:
            chip = ""
            title = inline(row[0])
            start = 1
        fields = "".join(
            f'<div class="tr-f"><span class="tr-k">{esc(head[i]) if i < len(head) else ""}</span>'
            f'<span class="tr-v">{inline(row[i])}</span></div>'
            for i in range(start, len(row))
            if row[i]
        )
        out.append(f'<div class="trow"><div class="tr-h">{chip}<span>{title}</span></div>{fields}</div>')
    return '<div class="tcards">' + "".join(out) + "</div>"


def render_blocks(bl) -> str:
    out = []
    for kind, payload in bl:
        if kind == "table":
            out.append(render_table(payload))
        elif kind == "ul":
            lis = "".join(f"<li>{inline(i)}</li>" for i in payload)
            out.append(f'<ul class="bl">{lis}</ul>')
        elif kind == "quote":
            out.append(f'<div class="notice">{inline(payload)}</div>')
        elif kind == "note":
            out.append(f'<div class="note-line">{inline(payload)}</div>')
        else:
            out.append(f'<p class="impact-p">{inline(payload)}</p>')
    return "\n".join(x for x in out if x)


def render_article(art: dict) -> str:
    meta_html = ""
    meta = art.get("meta", "")
    if meta:
        mm = re.match(r"^(.*?)\s*·\s*([0-9]{4}[^\s]*|[0-9]{4})\s*$", meta)
        if mm:
            meta_html = (
                f'<div class="art-meta"><span class="src">{inline(mm.group(1))}</span>'
                f'<span>{esc(mm.group(2))}</span></div>'
            )
        else:
            meta_html = f'<div class="art-meta"><span class="src">{inline(meta)}</span></div>'
    body_html = " ".join(inline(b) for b in art["body"])
    if art["title"] and art["url"]:
        t = f'<h3><a href="{esc(art["url"])}" target="_blank" rel="noopener">{inline(art["title"])}</a></h3>'
    elif art["title"]:
        t = f'<h3>{inline(art["title"])}</h3>'
    else:
        t = ""
    return f'      <div class="art">{t}{meta_html}<div class="art-body">{body_html}</div></div>'


def takeaway_rows(b: dict) -> str:
    rows = []
    for theme, text, src in b["takeaways"]:
        rows.append(
            f'      <div class="tw"><span class="tw-chip">{inline(theme)}</span>'
            f'<div class="tw-body"><div class="tw-text">{inline(text)}</div>'
            f'<div class="tw-src">{inline(src)}</div></div></div>'
        )
    return "\n".join(rows)


# ---------------------------------------------------------------- 日次ページ


def brief_body(b: dict) -> list:
    """ブリーフ本文 (テイクアウェイ〜インパクト分析)。日次ページとホームで共用。"""
    p = []
    if b["takeaways"]:
        p.append('    <div class="card hero">\n      <h2>🎯 本日の重要テイクアウェイ</h2>\n' + takeaway_rows(b) + "\n    </div>")
    if b["overseas"]:
        seg = ['    <div class="card">\n      <h2>🌍 海外ニュース</h2>']
        for sub, arts in b["overseas"]:
            seg.append(f'      <div class="sub-h">{inline(sub)}</div>')
            seg.extend(render_article(a) for a in arts)
        seg.append("    </div>")
        p.append("\n".join(seg))
    if b["domestic"] or b["domestic_note"]:
        seg = ['    <div class="card">\n      <h2>🇯🇵 国内ニュース</h2>']
        if b["domestic"]:
            seg.extend(render_article(a) for a in b["domestic"])
        else:
            seg.append(f'      <div class="notice">{inline(b["domestic_note"])}</div>')
        seg.append("    </div>")
        p.append("\n".join(seg))
    if b["x_items"]:
        seg = ['    <div class="card">\n      <h2>📱 X（旧Twitter）注目投稿</h2>']
        for kind, item in b["x_items"]:
            if kind == "notice":
                seg.append(f'      <div class="notice">{inline(item)}</div>')
            elif kind == "plain":
                seg.append(f'      <p class="x-plain">{inline(item)}</p>')
            else:
                if item["title"] and item["url"]:
                    name = f'<a href="{esc(item["url"])}" target="_blank" rel="noopener">{inline(item["title"])}</a>'
                elif item["title"]:
                    name = inline(item["title"])
                else:
                    name = ""
                mt = f' — {inline(item["meta"])}' if item["meta"] else ""
                body = "".join(f"<div>{inline(x)}</div>" for x in item["body"])
                seg.append(f'      <div class="quote"><div class="q-name">{name}{mt}</div>{body}</div>')
        seg.append("    </div>")
        p.append("\n".join(seg))
    if b["impact"]:
        seg = ['    <div class="card hero">\n      <h2>📊 ビジネスインパクト分析</h2>']
        for h, paras in b["impact"]:
            seg.append(f'      <div class="impact-h">{inline(h)}</div>')
            seg.extend(f'      <p class="impact-p">{inline(x)}</p>' for x in paras)
        seg.append("    </div>")
        p.append("\n".join(seg))
    return p


def pager(older, newer, l_old="← 前の日", l_new="次の日 →") -> str:
    if not older and not newer:
        return ""
    pg = ['    <div class="pager">']
    if older:
        pg.append(f'      <a class="pg" href="{older[0]}"><span>{l_old}</span><b>{esc(older[1])}</b></a>')
    if newer:
        pg.append(f'      <a class="pg right" href="{newer[0]}"><span>{l_new}</span><b>{esc(newer[1])}</b></a>')
    pg.append("    </div>")
    return "\n".join(pg)


def d_ref(b):
    return (f"{b['slug']}.html", f"{b['date']}（{weekday_of(b['date'])}）")


def render_brief(b: dict, dailies, weeklies, idx: int) -> str:
    p = [
        f"""    <header class="page-head">
      <div class="eyebrow">Daily Brief · {esc(SITE_SUB)}</div>
      <h1>{esc(b["date_label"])}</h1>
      <p class="sub">{brief_article_count(b)} 記事 ／ 自動収集・AI 生成</p>
    </header>"""
    ]
    p += brief_body(b)
    newer = d_ref(dailies[idx - 1]) if idx > 0 else None
    older = d_ref(dailies[idx + 1]) if idx + 1 < len(dailies) else None
    p.append(pager(older, newer))
    footer = b["footer"] or f"本ブリーフは自動収集・AI 生成により作成。配信日：{b['date']}"
    p.append(f'    <div class="foot">{inline(footer)} ／ 正本は Obsidian vault (02_NOTES/news-briefs/)。</div>')
    return shell(
        f"{b['date']} — {SITE_TITLE}",
        render_sidebar(dailies, weeklies, b["slug"]),
        "\n".join(x for x in p if x),
    )


# ---------------------------------------------------------------- 週次ページ


def render_weekly(w: dict, dailies, weeklies, idx: int) -> str:
    p = []
    p.append(
        f"""    <header class="page-head">
      <div class="eyebrow">Weekly Intelligence · {esc(SITE_SUB)}</div>
      <h1>{esc(w["week"])} 週次インテリジェンス</h1>
      <p class="sub">対象期間 {esc(w["period"])} ／ 日次ブリーフを前段入力に週単位で再構成</p>
    </header>"""
    )
    for h, items in w["sections"]:
        seg = [f'    <div class="card">\n      <h2>{inline(h)}</h2>']
        for kind, payload in items:
            if kind == "intro":
                seg.append(render_blocks(payload))
            else:
                if payload["url"]:
                    t = f'<h3><a href="{esc(payload["url"])}" target="_blank" rel="noopener">{inline(payload["title"])}</a></h3>'
                else:
                    t = f'<h3>{inline(payload["title"])}</h3>'
                seg.append(f'      <div class="art">{t}<div class="art-body">{render_blocks(payload["blocks"])}</div></div>')
        seg.append("    </div>")
        p.append("\n".join(seg))

    newer = weeklies[idx - 1] if idx > 0 else None
    older = weeklies[idx + 1] if idx + 1 < len(weeklies) else None
    p.append(
        pager(
            (f"{older['slug']}.html", older["week"]) if older else None,
            (f"{newer['slug']}.html", newer["week"]) if newer else None,
            "← 前の週",
            "次の週 →",
        )
    )
    p.append('    <div class="foot">Claude により自動生成。正本は Obsidian vault (02_NOTES/weeklies/)。</div>')
    return shell(
        f"{w['week']} — 週次インテリジェンス",
        render_sidebar(dailies, weeklies, w["slug"]),
        "\n".join(x for x in p if x),
    )


# ---------------------------------------------------------------- index / archive / search


def render_index(dailies, weeklies) -> str:
    """ホーム = その日の最新ブリーフをそのまま全文表示する。"""
    b = dailies[0]
    p = [
        f"""    <header class="page-head">
      <div class="eyebrow">最新のブリーフ · {esc(SITE_SUB)}</div>
      <h1>{esc(b["date_label"])}</h1>
      <p class="sub">{brief_article_count(b)} 記事 ／ 自動収集・AI 生成</p>
    </header>"""
    ]
    p += brief_body(b)
    older = d_ref(dailies[1]) if len(dailies) > 1 else None
    p.append(pager(older, ("archive.html", f"全 {len(dailies)} 本"), "← 前の日", "アーカイブ →"))
    footer = b["footer"] or f"本ブリーフは自動収集・AI 生成により作成。配信日：{b['date']}"
    p.append(f'    <div class="foot">{inline(footer)} ／ 正本は Obsidian vault (02_NOTES/news-briefs/)。</div>')
    return shell(SITE_TITLE, render_sidebar(dailies, weeklies, "index"), "\n".join(x for x in p if x))


def render_archive(dailies, weeklies) -> str:
    rows = []
    for b in dailies:
        rows.append((b["date"], "daily", b))
    for w in weeklies:
        rows.append((w["end"] or w["date"], "weekly", w))
    rows.sort(key=lambda r: (r[0], 0 if r[1] == "weekly" else 1), reverse=True)
    out, cur = [], None
    for date, kind, obj in rows:
        mo = date[:7]
        if mo != cur:
            cur = mo
            y, m = mo.split("-")
            out.append(f'      <div class="mo-h">{y}年{int(m)}月</div>')
        if kind == "daily":
            hl = clip(plain(obj["takeaways"][0][1]), 84) if obj["takeaways"] else ""
            out.append(
                f'      <a class="row" href="{obj["slug"]}.html"><span class="row-d">{obj["date"]}</span>'
                f'<span class="row-w">{weekday_of(obj["date"])}曜</span>'
                f'<span class="row-t">{esc(hl)}</span></a>'
            )
        else:
            hl = clip(plain(obj["summary"][0]), 84) if obj["summary"] else "週次インテリジェンス"
            out.append(
                f'      <a class="row wk" href="{obj["slug"]}.html"><span class="row-d">{esc(obj["week"])}</span>'
                f'<span class="row-w">週次</span><span class="row-t">{esc(hl)}</span></a>'
            )
    main = f"""    <header class="page-head">
      <div class="eyebrow">Archive</div>
      <h1>アーカイブ</h1>
      <p class="sub">デイリー {len(dailies)} 本 ／ 週次 {len(weeklies)} 本。新しい順。</p>
    </header>
    <div class="card">
{chr(10).join(out)}
    </div>"""
    return shell(f"アーカイブ — {SITE_TITLE}", render_sidebar(dailies, weeklies, "archive"), main)


SEARCH_JS = """
var IDX=[],Q=document.getElementById('q'),R=document.getElementById('res'),N=document.getElementById('cnt'),F='all';
fetch('search-index.json').then(function(r){return r.json()}).then(function(j){IDX=j;var p=new URLSearchParams(location.search).get('q');if(p){Q.value=p;run();}});
function esc(s){return s.replace(/[&<>]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c]})}
function snip(t,q){var i=t.toLowerCase().indexOf(q.toLowerCase());if(i<0)return '';
var s=Math.max(0,i-45),e=Math.min(t.length,i+q.length+95);
return (s>0?'…':'')+esc(t.slice(s,i))+'<mark>'+esc(t.slice(i,i+q.length))+'</mark>'+esc(t.slice(i+q.length,e))+(e<t.length?'…':'');}
function run(){var q=Q.value.trim();R.innerHTML='';if(!q){N.textContent='キーワードを入力してください。';return;}
var lq=q.toLowerCase(),hits=[];
IDX.forEach(function(d){if(F!=='all'&&d.k!==F)return;
var lines=[];d.x.forEach(function(l){if(l.toLowerCase().indexOf(lq)>=0)lines.push(l)});
if(lines.length)hits.push({d:d,n:lines.length,s:lines.slice(0,2)});});
N.textContent=hits.length?hits.length+' 件のブリーフでヒット（新しい順）':'該当なし。表記ゆれ（英語名・略称）も試してください。';
hits.forEach(function(h){var a=document.createElement('a');a.className='hit';a.href=h.d.u;
a.innerHTML='<div class="hit-h">'+esc(h.d.t)+' <span style="color:var(--text-dim);font-weight:600">· '+h.n+'箇所</span></div>'
+h.s.map(function(l){return '<div class="hit-s">'+snip(l,q)+'</div>'}).join('');R.appendChild(a);});}
Q.addEventListener('input',run);
document.querySelectorAll('.chip').forEach(function(c){c.addEventListener('click',function(){
if(c.dataset.f){document.querySelectorAll('.chip[data-f]').forEach(function(x){x.classList.remove('on')});c.classList.add('on');F=c.dataset.f;run();}
else{Q.value=c.textContent;run();Q.focus();}});});
"""


def render_search(dailies, weeklies) -> str:
    main = """    <header class="page-head">
      <div class="eyebrow">Search</div>
      <h1>全文検索</h1>
      <p class="sub">テイクアウェイ・記事本文・週次レポートを横断して検索します（日本語・英語どちらでも）。</p>
    </header>
    <div class="card">
      <div class="sbox"><input id="q" type="search" placeholder="例: ICON、book-to-bill、PMDA、Anthropic" autocomplete="off"></div>
      <div class="chips">
        <button class="chip on" data-f="all" type="button">すべて</button>
        <button class="chip" data-f="daily" type="button">デイリーのみ</button>
        <button class="chip" data-f="weekly" type="button">週次のみ</button>
      </div>
      <div class="chips" style="margin-top:14px">
        <button class="chip" type="button">ICON</button>
        <button class="chip" type="button">IQVIA</button>
        <button class="chip" type="button">Anthropic</button>
        <button class="chip" type="button">PMDA</button>
        <button class="chip" type="button">book-to-bill</button>
        <button class="chip" type="button">エージェント</button>
      </div>
      <div class="hits" id="cnt">キーワードを入力してください。</div>
      <div id="res"></div>
    </div>"""
    return shell(
        f"検索 — {SITE_TITLE}",
        render_sidebar(dailies, weeklies, "search"),
        main,
        extra_js=SEARCH_JS,
        topsearch=False,
    )


def build_search_index(dailies, weeklies):
    docs = []
    for b in dailies:
        lines = []
        for theme, text, src in b["takeaways"]:
            lines.append(f"{plain(theme)}｜{plain(text)}")
        for sub, arts in b["overseas"]:
            for a in arts:
                lines.append(plain(a["title"] or ""))
                lines.extend(plain(x) for x in a["body"])
        for a in b["domestic"]:
            lines.append(plain(a["title"] or ""))
            lines.extend(plain(x) for x in a["body"])
        for h, paras in b["impact"]:
            lines.extend(plain(x) for x in paras)
        docs.append(
            {
                "d": b["date"],
                "u": f"{b['slug']}.html",
                "t": f"{b['date']}（{weekday_of(b['date'])}）デイリー",
                "k": "daily",
                "x": [l for l in lines if len(l) > 3],
            }
        )
    for w in weeklies:
        lines = []
        for h, items in w["sections"]:
            for kind, payload in items:
                if kind == "intro":
                    lines.append(blocks_text(payload))
                else:
                    lines.append(plain(payload["title"]))
                    lines.append(blocks_text(payload["blocks"]))
        docs.append(
            {
                "d": w["end"] or w["date"],
                "u": f"{w['slug']}.html",
                "t": f"{w['week']} 週次インテリジェンス",
                "k": "weekly",
                "x": [l for l in lines if len(l) > 3],
            }
        )
    docs.sort(key=lambda d: (d["d"], 0 if d["k"] == "daily" else 1), reverse=True)
    return docs


# ---------------------------------------------------------------- main


def main():
    argv = sys.argv[1:]
    publish = "--publish" in argv
    out = REPO
    if "--out" in argv:
        out = Path(argv[argv.index("--out") + 1]).resolve()
        out.mkdir(parents=True, exist_ok=True)

    dsrc = sorted(p for p in VAULT_BRIEFS.glob("*_daily-ai-news-brief.md") if p.name[:10] >= SITE_START)
    if not dsrc:
        print("no source briefs found")
        return 1
    dailies = [parse_brief(p) for p in dsrc]
    dailies.sort(key=lambda b: b["date"], reverse=True)

    weeklies = []
    if VAULT_WEEKLIES.exists():
        for p in sorted(VAULT_WEEKLIES.glob("*-Intelligence.md")):
            w = parse_weekly(p)
            if (w["end"] or w["date"]) >= SITE_START:
                weeklies.append(w)
    weeklies.sort(key=lambda w: w["week"], reverse=True)

    for i, b in enumerate(dailies):
        (out / f"{b['slug']}.html").write_text(render_brief(b, dailies, weeklies, i), encoding="utf-8")
    print(f"built {len(dailies)} daily pages")
    for i, w in enumerate(weeklies):
        (out / f"{w['slug']}.html").write_text(render_weekly(w, dailies, weeklies, i), encoding="utf-8")
    print(f"built {len(weeklies)} weekly pages")

    (out / "index.html").write_text(render_index(dailies, weeklies), encoding="utf-8")
    (out / "archive.html").write_text(render_archive(dailies, weeklies), encoding="utf-8")
    (out / "search.html").write_text(render_search(dailies, weeklies), encoding="utf-8")
    idx = build_search_index(dailies, weeklies)
    (out / "search-index.json").write_text(
        json.dumps(idx, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    (out / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    kb = (out / "search-index.json").stat().st_size / 1024
    print(f"built index/archive/search (search-index.json {kb:.0f} KB)")

    valid = {f"{b['slug']}.html" for b in dailies} | {f"{w['slug']}.html" for w in weeklies}
    for f in list(out.glob("brief-*.html")) + list(out.glob("weekly-*.html")):
        if f.name not in valid:
            f.unlink()
            print(f"removed stale {f.name}")

    if publish:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        subprocess.run(["git", "add", "-A"], cwd=out, check=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=out).returncode == 0:
            print("no changes to publish")
            return 0
        msg = f"Auto-update: daily brief site ({ts})\n\nCo-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
        subprocess.run(["git", "commit", "-m", msg], cwd=out, check=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=out, check=True)
        print("pushed to origin/main")
    return 0


if __name__ == "__main__":
    sys.exit(main())
