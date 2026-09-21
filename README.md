# DeepSeek RSS Feed

Unofficial RSS feeds for the public news pages on the official DeepSeek website.

Source pages:

- Chinese: https://www.deepseek.com/news/
- English: https://www.deepseek.com/en/news/

## Feeds

After pushing this repository to GitHub, subscribe to:

### 中文

```text
https://raw.githubusercontent.com/junphy-jan/deepseek-rss-feed/main/deepseek_news_zh_rss.xml
```

### English

```text
https://raw.githubusercontent.com/junphy-jan/deepseek-rss-feed/main/deepseek_news_en_rss.xml
```

## Features

- Separate Chinese (`zh-CN`) and English (`en`) feeds.
- Scrapes the official DeepSeek news index with Playwright.
- Extracts article URL, title, publication date, and summary.
- Deduplicates entries by canonical URL.
- Sorts newest articles first.
- Keeps up to 50 items per feed.
- Uses atomic file replacement.
- Fails closed if the scraper returns zero articles, so a temporary outage or DOM change cannot erase a previously valid feed.
- Runs every 6 hours with GitHub Actions.
- Supports manual execution via `Actions -> Update DeepSeek RSS feeds -> Run workflow`.
- No API key or external service is required.

## Local development

```bash
python -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
python -m playwright install chromium

python deepseek_rss.py
```

The two XML files will be generated in the repository root.

## Update schedule

GitHub Actions runs every 6 hours. GitHub scheduled workflows are not guaranteed to start at the exact minute, so a small delay is normal.

## Disclaimer

This is an unofficial community project and is not affiliated with DeepSeek.

It aggregates publicly available information from the official DeepSeek website. Please refer to DeepSeek's website for the authoritative source.
