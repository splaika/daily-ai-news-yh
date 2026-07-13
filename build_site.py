# -*- coding: utf-8 -*-
"""
build_site.py — Obsidian の日次ブリーフ md から GitHub Pages サイトを全量再生成する。

  ソース: C:\\Users\\yasu\\Obsidian Vault desktop\\02_NOTES\\news-briefs\\YYYY-MM-DD_daily-ai-news-brief.md
  出力:   このリポジトリ直下の brief-YYYY-MM-DD.html / index.html / robots.txt
  対象:   SITE_START (2026-07-01) 以降の日付のみ。それより古い brief-*.html は削除する。
  除外:   「💬 私のメモ」セクション (個人入力欄のため公開しない)

使い方:
  python build_site.py            # 生成のみ
  python build_site.py --publish  # 生成 + git add/commit/push (変更がない日は push しない)

毎朝の Routine `daily-ai-news-brief` が Obsidian へ md を書いた後に --publish で呼ぶ。
冪等 (全量再生成) なので手動で何度実行してもよい。
"""
import re
import sys
import subprocess
import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

VAULT_BRIEFS = Path(r"C:\Users\yasu\Obsidian Vault desktop\02_NOTES\news-briefs")
REPO = Path(__file__).resolve().parent
SITE_START = "2026-07-01"
SITE_TITLE = "Daily AI News Brief — 医薬品開発 × CRO"

# ---------------------------------------------------------------- helpers

def esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def inline(t: str) -> str:
    """インライン markdown (リンク・太字) を HTML に変換。先に HTML エスケープする。"""
    t = esc(t)
    t = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        r'<a href="\2" target="_blank" rel="noopener">\1</a>',
        t,
    )
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    return t


def strip_md(t: str) -> str:
    """index カードの抜粋用: markdown 記法を落として素のテキストに。"""
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    return t.replace("**", "").strip()


# ---------------------------------------------------------------- parsing

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
            theme = cells[0]
            src = cells[-1]
            text = " | ".join(cells[1:-1])
            rows.append((theme, text, src))
    return rows


def parse_articles(body: str):
    """`- **[title](url)** — meta` + インデント継続行(要約) のリストをパース。"""
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
            elif m2:  # リンクなし太字見出し (`- **タイトル** — 出典`)
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
    """### サブセクション名 → 記事リスト の順序付きリスト。"""
    groups = []
    parts = re.split(r"^### ", body, flags=re.M)
    for p in parts[1:]:
        header, _, sub = p.partition("\n")
        groups.append((header.strip(), parse_articles(sub)))
    return groups


def parse_impact(body: str):
    groups = []
    parts = re.split(r"^### ", body, flags=re.M)
    for p in parts[1:]:
        header, _, sub = p.partition("\n")
        paras = [l.strip() for l in sub.splitlines() if l.strip() and not l.strip().startswith("---") and not l.strip().startswith("*本ブリーフ")]
        groups.append((header.strip(), paras))
    return groups


def parse_x_section(body: str):
    """X 欄は日によって形式が揺れる (実投稿あり / 注記のみ)。行グループ単位で汎用パース。"""
    items = []  # ("notice", text) / ("post", art) / ("plain", text)
    notice_buf = []
    arts_body = []
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


def parse_brief(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    date = path.name[:10]
    m = re.search(r"^# .*?\|\s*(.+?)\s*$", text, re.M)
    date_label = m.group(1) if m else date
    secs = split_sections(text)
    footer = ""
    fm = re.findall(r"^\*([^*].*?)\*\s*$", text, re.M)
    if fm:
        footer = fm[-1]
    return {
        "date": date,
        "date_label": date_label,
        "takeaways": parse_takeaways(section_by_emoji(secs, "📌")),
        "overseas": parse_overseas(section_by_emoji(secs, "🌍")),
        "domestic": parse_articles(section_by_emoji(secs, "🇯🇵")),
        "x_items": parse_x_section(section_by_emoji(secs, "📱")),
        "impact": parse_impact(section_by_emoji(secs, "📊")),
        "footer": footer,
    }


# ---------------------------------------------------------------- rendering

CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
:root {
  --bg: #0f1117; --bg-card: #1a1d2e; --bg-sidebar: #141625; --border: #2a2d3e;
  --text: #e4e6f0; --text-dim: #8b8fa3; --accent: #6c7bf7; --accent-light: #8b97ff;
  --yellow: #ffd93d; --yellow-bg: rgba(255,217,61,0.1); --blue: #4ecdc4;
}
html, body { overflow-x: hidden; }
body { font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, sans-serif; background: var(--bg); color: var(--text); line-height: 1.7; }
a { color: var(--accent-light); }
.layout { display: grid; grid-template-columns: 240px 1fr; min-height: 100vh; }
.sidebar { background: var(--bg-sidebar); border-right: 1px solid var(--border); position: sticky; top: 0; height: 100vh; overflow-y: auto; }
.logo { padding: 22px 20px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 10px; }
.logo-icon { font-size: 26px; }
.logo-text { font-size: 16px; font-weight: 700; color: var(--accent-light); }
.nav { padding: 12px 8px; }
.nav-section-title { padding: 14px 12px 6px; font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: var(--text-dim); }
.nav-item { display: flex; align-items: center; gap: 8px; padding: 8px 12px; border-radius: 8px; color: var(--text-dim); text-decoration: none; font-size: 14px; }
.nav-item:hover { background: rgba(108,123,247,0.1); color: var(--text); }
.nav-item.active { background: rgba(108,123,247,0.15); color: var(--accent-light); font-weight: 600; }
.main { padding: 24px 32px; min-width: 0; }
.header { margin-bottom: 24px; }
.header h1 { font-size: 26px; font-weight: 700; line-height: 1.3; }
.subtitle { color: var(--text-dim); font-size: 14px; margin-top: 4px; }
.card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px; padding: 18px 20px; margin-bottom: 16px; max-width: 100%; overflow: hidden; }
.card h2 { font-size: 18px; margin-bottom: 14px; display: flex; align-items: center; gap: 8px; }
.highlight-card { border-left: 4px solid var(--accent); }
.highlight-card p { font-size: 15px; }
.sub-h { font-size: 13px; text-transform: uppercase; letter-spacing: 1px; color: var(--text-dim); margin: 18px 0 4px; }
.article-card { border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; margin: 10px 0; }
.article-card h3 { font-size: 15px; font-weight: 600; line-height: 1.5; }
.article-card h3 a { color: var(--text); text-decoration: none; }
.article-card h3 a:hover { color: var(--accent-light); }
.article-meta { display: flex; flex-wrap: wrap; gap: 8px 12px; align-items: center; font-size: 12px; color: var(--text-dim); margin: 6px 0 8px; }
.source-badge { background: rgba(108,123,247,0.12); color: var(--accent-light); border-radius: 10px; padding: 1px 8px; }
.article-body { font-size: 14px; }
.takeaway-item { display: flex; gap: 10px; align-items: flex-start; padding: 10px 0; border-bottom: 1px solid var(--border); flex-wrap: wrap; }
.takeaway-item:last-child { border-bottom: none; }
.tw-chip { flex-shrink: 0; font-size: 11px; padding: 2px 8px; border-radius: 10px; background: rgba(108,123,247,0.15); color: var(--accent-light); font-weight: 600; white-space: nowrap; margin-top: 3px; }
.tw-body { flex: 1 1 240px; min-width: 0; }
.tw-text { font-size: 14px; }
.tw-src { font-size: 12px; margin-top: 2px; }
.tw-src a { color: var(--blue); text-decoration: none; }
.tw-src a:hover { text-decoration: underline; }
.quote-card { border-left: 3px solid var(--yellow); padding: 10px 14px; margin: 10px 0; background: rgba(255,217,61,0.05); border-radius: 0 8px 8px 0; }
.quote-card .q-name { font-weight: 600; color: var(--yellow); font-size: 14px; }
.quote-card .q-name a { color: var(--yellow); }
.quote-card .q-text { font-size: 14px; margin: 4px 0; }
.notice { font-size: 13px; color: var(--text-dim); background: var(--yellow-bg); border: 1px solid rgba(255,217,61,0.25); border-radius: 8px; padding: 10px 14px; margin: 10px 0; }
.x-plain { font-size: 13px; color: var(--text-dim); margin: 8px 0; }
.impact-h { font-size: 15px; font-weight: 600; color: var(--accent-light); margin: 14px 0 6px; }
.impact-p { font-size: 14px; }
.page-footer { color: var(--text-dim); font-size: 12px; margin-top: 28px; border-top: 1px solid var(--border); padding-top: 14px; }
.brief-grid { display: grid; gap: 14px; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); }
.brief-card-link { text-decoration: none; }
.brief-card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px; padding: 16px 18px; height: 100%; transition: border-color 0.15s; }
.brief-card:hover { border-color: var(--accent); }
.brief-date { font-size: 16px; font-weight: 700; color: var(--accent-light); }
.brief-meta { font-size: 12px; color: var(--text-dim); margin: 2px 0 8px; }
.brief-highlight { font-size: 13px; color: var(--text); }
.mobile-header { display: none; }
@media (max-width: 900px) {
  .layout { grid-template-columns: 1fr; }
  .mobile-header { display: flex; align-items: center; gap: 10px; position: fixed; top: 0; left: 0; right: 0; z-index: 20; background: var(--bg-sidebar); border-bottom: 1px solid var(--border); padding: 10px 14px; }
  .hamburger { background: none; border: 1px solid var(--border); border-radius: 8px; color: var(--text); font-size: 18px; padding: 4px 10px; }
  .sidebar { position: fixed; left: -260px; width: 250px; z-index: 30; transition: left 0.2s; }
  .sidebar.open { left: 0; }
  .sidebar-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 25; }
  .sidebar-overlay.active { display: block; }
  .main { padding: 14px; padding-top: 66px; }
}
"""

MOBILE_JS = """
const menuBtn = document.getElementById('menuBtn');
const sidebarEl = document.getElementById('sidebar');
const overlayEl = document.getElementById('sidebarOverlay');
menuBtn.addEventListener('click', () => { sidebarEl.classList.toggle('open'); overlayEl.classList.toggle('active'); });
overlayEl.addEventListener('click', () => { sidebarEl.classList.remove('open'); overlayEl.classList.remove('active'); });
"""


def render_sidebar(dates, active: str) -> str:
    rows = []
    for d in dates:
        cls = " active" if d == active else ""
        rows.append(f'      <a href="brief-{d}.html" class="nav-item{cls}"><span>📄</span>{d}</a>')
    index_cls = " active" if active == "index" else ""
    return f"""<div class="mobile-header">
  <button class="hamburger" id="menuBtn" aria-label="メニュー">☰</button>
  <div class="logo-icon">🧠</div>
  <div class="logo-text">AI Intel Brief</div>
</div>
<div class="sidebar-overlay" id="sidebarOverlay"></div>
<div class="layout">
  <aside class="sidebar" id="sidebar">
    <div class="logo">
      <div class="logo-icon">🧠</div>
      <div class="logo-text">AI Intel Brief</div>
    </div>
    <nav class="nav">
      <a href="index.html" class="nav-item{index_cls}"><span>🗂</span>ブリーフ一覧</a>
      <div class="nav-section-title">デイリーブリーフ</div>
{chr(10).join(rows)}
    </nav>
  </aside>"""


def page_shell(title: str, sidebar: str, main_html: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<title>{esc(title)}</title>
<style>{CSS}</style>
</head>
<body>
{sidebar}
  <main class="main">
{main_html}
  </main>
</div>
<script>{MOBILE_JS}</script>
</body>
</html>
"""


def render_article(art: dict) -> str:
    meta_html = ""
    meta = art.get("meta", "")
    if meta:
        mm = re.match(r"^(.*?)\s*·\s*([0-9]{4}[^\s]*|[0-9]{4})\s*$", meta)
        if mm:
            meta_html = f'<div class="article-meta"><span>📅 {esc(mm.group(2))}</span><span class="source-badge">{inline(mm.group(1))}</span></div>'
        else:
            meta_html = f'<div class="article-meta"><span class="source-badge">{inline(meta)}</span></div>'
    body_html = " ".join(inline(b) for b in art["body"])
    if art["title"] and art["url"]:
        title_html = f'<h3><a href="{esc(art["url"])}" target="_blank" rel="noopener">{inline(art["title"])}</a></h3>'
    elif art["title"]:
        title_html = f'<h3>{inline(art["title"])}</h3>'
    else:
        title_html = ""
    return f"""      <div class="article-card">
        {title_html}
        {meta_html}
        <div class="article-body">{body_html}</div>
      </div>"""


def render_brief(brief: dict, dates) -> str:
    parts = []
    parts.append(
        f"""    <header class="header">
      <h1>{SITE_TITLE}</h1>
      <p class="subtitle">{esc(brief["date_label"])} · 自動収集・AI生成</p>
    </header>"""
    )
    # takeaways
    if brief["takeaways"]:
        rows = []
        for theme, text, src in brief["takeaways"]:
            rows.append(
                f"""      <div class="takeaway-item">
        <span class="tw-chip">{inline(theme)}</span>
        <div class="tw-body">
          <div class="tw-text">{inline(text)}</div>
          <div class="tw-src">{inline(src)}</div>
        </div>
      </div>"""
            )
        parts.append('    <div class="card">\n      <h2>🎯 本日の重要テイクアウェイ</h2>\n' + "\n".join(rows) + "\n    </div>")
    # overseas
    if brief["overseas"]:
        seg = ['    <div class="card">\n      <h2>🌍 海外ニュース</h2>']
        for sub, arts in brief["overseas"]:
            seg.append(f'      <div class="sub-h">{inline(sub)}</div>')
            seg.extend(render_article(a) for a in arts)
        seg.append("    </div>")
        parts.append("\n".join(seg))
    # domestic
    if brief["domestic"]:
        seg = ['    <div class="card">\n      <h2>🇯🇵 国内ニュース</h2>']
        seg.extend(render_article(a) for a in brief["domestic"])
        seg.append("    </div>")
        parts.append("\n".join(seg))
    # X
    if brief["x_items"]:
        seg = ['    <div class="card">\n      <h2>📱 X（旧Twitter）注目投稿</h2>']
        for kind, item in brief["x_items"]:
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
                meta = f' — {inline(item["meta"])}' if item["meta"] else ""
                body = "".join(f'<div class="q-text">{inline(b)}</div>' for b in item["body"])
                seg.append(
                    f"""      <div class="quote-card">
        <div class="q-name">{name}{meta}</div>
        {body}
      </div>"""
                )
        seg.append("    </div>")
        parts.append("\n".join(seg))
    # impact
    if brief["impact"]:
        seg = ['    <div class="card highlight-card">\n      <h2>📊 ビジネスインパクト分析</h2>']
        for h, paras in brief["impact"]:
            seg.append(f'      <div class="impact-h">{inline(h)}</div>')
            seg.extend(f'      <p class="impact-p">{inline(p)}</p>' for p in paras)
        seg.append("    </div>")
        parts.append("\n".join(seg))
    # footer
    footer = brief["footer"] or f"本ブリーフは自動収集・AI生成により作成。配信日：{brief['date']}"
    parts.append(f'    <div class="page-footer">{inline(footer)} ／ 正本は Obsidian vault (02_NOTES/news-briefs/)。</div>')
    main_html = "\n".join(parts)
    return page_shell(f"AI Brief - {brief['date']}", render_sidebar(dates, brief["date"]), main_html)


def render_index(briefs, dates) -> str:
    cards = []
    for b in briefs:  # briefs は新しい順
        n_articles = sum(len(a) for _, a in b["overseas"]) + len(b["domestic"])
        hl = strip_md(b["takeaways"][0][1]) if b["takeaways"] else ""
        if len(hl) > 110:
            hl = hl[:110] + "…"
        weekday = ""
        wm = re.search(r"（(.+?)）", b["date_label"])
        if wm:
            weekday = f"（{wm.group(1)}）"
        cards.append(
            f"""        <a href="brief-{b['date']}.html" class="brief-card-link">
          <div class="brief-card">
            <div class="brief-date">{b['date']}{esc(weekday)}</div>
            <div class="brief-meta">{n_articles}記事 / 医薬品開発×CRO</div>
            <div class="brief-highlight">{esc(hl)}</div>
          </div>
        </a>"""
        )
    main_html = f"""    <header class="header">
      <h1>{SITE_TITLE}</h1>
      <p class="subtitle">Obsidian の日次ブリーフ (Opus Routine 自動生成) の公開ミラー。2026年7月以降を掲載。</p>
    </header>
    <div class="card">
      <h2>📅 ブリーフ一覧</h2>
      <div class="brief-grid">
{chr(10).join(cards)}
      </div>
    </div>
    <div class="page-footer">自動収集・AI生成 ／ 正本は Obsidian vault (02_NOTES/news-briefs/)。</div>"""
    return page_shell(SITE_TITLE, render_sidebar(dates, "index"), main_html)


# ---------------------------------------------------------------- main

def main():
    publish = "--publish" in sys.argv
    sources = sorted(
        p for p in VAULT_BRIEFS.glob("*_daily-ai-news-brief.md") if p.name[:10] >= SITE_START
    )
    if not sources:
        print("no source briefs found")
        return 1
    briefs = [parse_brief(p) for p in sources]
    briefs.sort(key=lambda b: b["date"], reverse=True)
    dates = [b["date"] for b in briefs]

    for b in briefs:
        out = REPO / f"brief-{b['date']}.html"
        out.write_text(render_brief(b, dates), encoding="utf-8")
        print(f"built {out.name}")
    (REPO / "index.html").write_text(render_index(briefs, dates), encoding="utf-8")
    print("built index.html")
    (REPO / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")

    # SITE_START より古い・ソースが消えた brief-*.html は削除 (全量再生成の一貫性)
    valid = {f"brief-{d}.html" for d in dates}
    for f in REPO.glob("brief-*.html"):
        if f.name not in valid:
            f.unlink()
            print(f"removed stale {f.name}")

    if publish:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        subprocess.run(["git", "add", "-A"], cwd=REPO, check=True)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO)
        if diff.returncode == 0:
            print("no changes to publish")
            return 0
        msg = f"Auto-update: daily brief site ({ts})\n\nCo-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
        subprocess.run(["git", "commit", "-m", msg], cwd=REPO, check=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=REPO, check=True)
        print("pushed to origin/main")
    return 0


if __name__ == "__main__":
    sys.exit(main())
