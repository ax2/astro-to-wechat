# astro-to-wechat

Sync a published article into the WeChat Official Account draft box.

This repository contains a standalone sync script, a safe config example, and setup docs. No AppID, AppSecret, local config, or generated payloads are committed.

## What It Does

- Fetches a published article page by URL or Astro `slug`
- Extracts the article body and rewrites images for WeChat
- Uploads inline images to WeChat
- Creates or updates a draft in the WeChat draft box
- Optionally submits the draft for publish
- Optionally uses frontmatter fields like `wechatDraftMediaId` and `wechatPublishId`

## Repository Layout

- `scripts/sync_wechat_article.py`: main sync script
- `config/wechat.example.json`: config template
- `package.json`: optional Node dependency for SVG to PNG conversion via `sharp`

## Requirements

- Python 3.10+
- Node.js 18+ if you want automatic SVG to PNG conversion for cover images
- A WeChat Official Account with `app_id` and `app_secret`

## Setup

1. Clone the repository:

```bash
git clone https://github.com/ax2/astro-to-wechat.git
cd astro-to-wechat
```

2. Install the optional Node dependency used for SVG cover conversion:

```bash
npm install
```

You can then use the packaged shortcut:

```bash
npm run sync:wechat -- --help
```

3. Copy the config example:

```bash
cp config/wechat.example.json config/wechat.local.json
```

4. Edit `config/wechat.local.json`.

## Config

Example:

```json
{
  "wechat": {
    "app_id": "your-wechat-app-id",
    "app_secret": "your-wechat-app-secret",
    "site_url": "https://example.com",
    "author": "Your Name",
    "thumb_image_path": "public/wechat-thumb.jpg",
    "open_comment": 0,
    "fans_can_comment": 0,
    "content_root": "../your-astro-site/src/content/blog",
    "public_root": "../your-astro-site/public",
    "node_modules_root": "."
  }
}
```

Field notes:

- `app_id`: WeChat Official Account AppID
- `app_secret`: WeChat Official Account AppSecret
- `site_url`: your public site root, used to resolve relative URLs and `--slug`
- `author`: default author shown in WeChat
- `thumb_image_path`: fallback cover image path or URL
- `open_comment`: `1` to enable comments, otherwise `0`
- `fans_can_comment`: `1` to restrict comments to followers, otherwise `0`
- `content_root`: optional Astro content directory, used to find a source file by `slug` and write back `wechatDraftMediaId` or `wechatPublishId`
- `public_root`: optional static asset directory, used to resolve local images like `/wechat-thumb.jpg`
- `node_modules_root`: optional directory containing installed Node packages; usually `"."`

If `content_root` is omitted, the script still works, but it will not auto-locate the source markdown file for frontmatter writeback.

## Usage

Preview generated payload without calling the WeChat API:

```bash
python3 scripts/sync_wechat_article.py --slug your-article-slug --dry-run --output tmp/wechat-preview.json
```

Equivalent command:

```bash
npm run sync:wechat -- --slug your-article-slug --dry-run --output tmp/wechat-preview.json
```

Create a new draft:

```bash
python3 scripts/sync_wechat_article.py --slug your-article-slug
```

Use a full URL instead of a slug:

```bash
python3 scripts/sync_wechat_article.py https://example.com/blog/your-article/
```

Update an existing draft:

```bash
python3 scripts/sync_wechat_article.py --slug your-article-slug --update-media-id MEDIA_ID
```

Publish after creating or updating a draft:

```bash
python3 scripts/sync_wechat_article.py --slug your-article-slug --publish
```

Publish an existing draft directly:

```bash
python3 scripts/sync_wechat_article.py --publish-existing-media-id MEDIA_ID
```

Create a draft and send it to all followers:

```bash
python3 scripts/sync_wechat_article.py --slug your-article-slug --mode sendall
```

Send only to a specific fan tag:

```bash
python3 scripts/sync_wechat_article.py --slug your-article-slug --mode sendall --tag-id 123
```

## Behavior Notes

- If a markdown file under `content_root` already contains `wechatDraftMediaId`, the script reuses it as the update target when `--update-media-id` is not explicitly provided.
- When updating a draft, the original draft cover is reused unless you explicitly pass `--thumb`.
- Inline images that cannot be uploaded are left as their original URLs.
- Cover images must be in a WeChat-supported format. SVG covers can be converted to PNG automatically if `sharp` is installed.
- Relative `--output` paths are written under this repository.

## Security

- Do not commit `config/wechat.local.json`.
- Do not commit real `app_id` or `app_secret`.
- Review generated payloads in `tmp/` before sharing them.

The repository ignores local config and generated preview files by default.
