# Daily AI News Brief — 医薬品開発 × CRO

Obsidian の日次ブリーフ（Opus Routine `daily-ai-news-brief` が毎朝 08:05 に生成）の公開ミラー。

**公開ページ**: https://splaika.github.io/daily-ai-news-yh/

## 構成

- 正本: `C:\Users\yasu\Obsidian Vault desktop\02_NOTES\news-briefs\YYYY-MM-DD_daily-ai-news-brief.md`
- 本リポジトリはそこからの**全量再生成**（2026-07-01 以降のみ掲載。「💬 私のメモ」欄は公開しない）
- 2026-06-15 以前の旧ローカルパイプライン版（スコア/ブックマーク付きダッシュボード形式）は 2026-07-14 に削除（git 履歴には残存）

## 更新方法

```
python build_site.py            # 生成のみ（冪等・全量再生成）
python build_site.py --publish  # 生成 + git commit/push（変更なしなら push しない）
```

毎朝の Routine が Obsidian へ md を書いた後に `--publish` で実行する。手動実行も可。
push 後 1〜2 分で GitHub Pages に反映される。
