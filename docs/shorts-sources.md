# Where football shorts can legitimately come from

Research notes for the shorts pipeline, November 2026. The question was: can we
scrape football clips from Instagram, TikTok and similar, and store direct MP4
links instead of YouTube embeds?

**Short answer: not from the social platforms.** Two independent blockers, one
legal and one technical, and the technical one alone would break the feature.

## The technical blocker: those URLs expire

Instagram and TikTok serve media from signed CDN URLs that carry an expiry.
Storing one in a database gives you a row that plays for a few hours and then
404s. Meta's own documentation says its CDN URLs stop returning media once the
content is deleted or the link has expired, and third-party integrators
recommend storing the post's shortcode and re-requesting a fresh link, or
taking a copy at ingestion time, precisely because of this.

So "store the MP4 link" cannot work against a social CDN, whatever the rights
situation. It only works against a provider whose URLs are stable.

## The legal blocker: the APIs do not offer the file

| Platform | What the official API gives | Direct MP4? |
| --- | --- | --- |
| Instagram | Graph API `media_url`, only for accounts you own or manage | No for most video: Meta omits `media_url` when a video contains copyrighted or licensed audio, which covers nearly all football clips |
| TikTok | Display API returns `embed_link`; playback is designed for the iframe Embed Player | No documented file access. "Embed Videos" is being retired in favour of Embed Player |
| YouTube | Data API returns metadata; playback via the embed player | No |

None of these expose a downloadable file for other people's content, and
scraping around that is both a terms violation and, for broadcaster footage, a
copyright one. Football highlights are owned by leagues and broadcasters; a
public endpoint returning an MP4 does not mean you may store or redistribute
it.

## Options that do work

Ranked by how well they fit this project.

### 1. A licensed rights feed — the only real answer for match highlights

Sportradar, Stats Perform, SNTV, or a direct league/broadcaster deal. You get
stable URLs or file delivery, written redistribution rights, and metadata that
matches fixtures. Paid, contract-based, and the only route that legitimately
puts broadcaster footage in your own player.

Once you have one, this repo already supports it:

```bash
manage.py fetch_shorts --from-file partner-feed.json            # download and host
manage.py fetch_shorts --from-file partner-feed.json --remote   # store links only
```

### 2. Highlightly — closest thing to a drop-in highlights API

Advertises real-time football highlights across 950+ leagues, available
directly or via RapidAPI, with a highlights endpoint documented as returning a
`video_url` pointing at an MP4. The published samples use placeholder URLs, and
the docs do not state whether those files may be downloaded, cached or
redistributed.

**Before building on it, get written confirmation of download and
redistribution rights.** If it only permits linking, use `--remote` mode, which
is exactly the shape this provider fits.

**Implemented** as `manage.py sync_highlightly` (client in `api/providers.py`).
Reading the endpoint docs changed the design: the response's `source` field is
`youtube`, `twitter`, `reddit` or `espn`, so most rows are **links to a player,
not MP4 files**. The command therefore classifies each row by its URL — real
MP4s become `remote` clips, everything else becomes a `link` — and skips
YouTube by default. `--only-mp4` keeps nothing but video files.

Written against the documented schema and tested against it, but **not yet run
against the live API** — that needs a key. Expect to adjust once real responses
are seen, particularly how many rows actually carry a direct MP4.

### 3. ScoreBat — easy, but embeds and branding

A video API for goals and highlights from the major leagues, embedding clips
from official YouTube channels and club sites. The free plan is limited to one
"Free Feed" endpoint and carries ScoreBat logos and ads; paid tiers run $69 and
$299 per month. Delivers **iframe embed codes, not MP4 files** — so it lands in
`link` mode, which you have ruled out.

### 4. Pexels / Pixabay — real MP4 links, but not match footage

Free commercial use, no attribution legally required (Pexels asks for credit
and a link back), and the API returns a `video_files` array with
`file_type: video/mp4` and a direct `link`. Genuinely usable with `--remote`.

The catch: this is **generic stock football footage** — training, crowds,
anonymous players — not match highlights. Fine for backgrounds, filler or
placeholder content; it will not satisfy a user who wants to see last night's
goals. Pexels' MP4 links are Vimeo-hosted and carry an `oauth2` token, so check
whether they stay valid before relying on them long-term.

### 5. Your own footage

Anything you shoot or edit yourself. `--remote` if you host it on a CDN,
default mode if you want the platform to hold the file.

## Recommendation

1. For **match highlights**: license a feed. There is no free, legal scrape.
   Highlightly is the cheapest thing to evaluate; confirm the rights in writing
   first.
2. Use **`--remote`** so the database holds the provider's MP4 URL and your
   disk stays empty — but only against a provider with stable URLs. Never
   against a social CDN.
3. Keep **`file` mode** for anything you own or have licensed, where you want
   control over availability and no dependency on someone else's CDN.
4. Treat `link` mode as a fallback for providers that only permit embedding.

## Sources

- [Instagram Platform — IG Media reference](https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/v21.0)
- [Instagram Platform overview](https://developers.facebook.com/documentation/instagram-platform/overview)
- [How to deal with expiring images of TikTok and Instagram (Iframely)](https://iframely.com/help/015914-how-to-deal-with-expiring-images-of-tik-tok-and-instagram.md.md.md.md.md.md.md.md.md.md.md.md.md.md.md.md)
- [TikTok Display API overview](https://developers.tiktok.com/doc/display-api-overview/)
- [TikTok Embed Player](https://developers.tiktok.com/docs/en/embed-player)
- [ScoreBat Video API](https://www.scorebat.com/video-api/)
- [Highlightly football API documentation](https://highlightly.net/documentation/football)
- [Pexels — what is the license](https://help.pexels.com/hc/en-us/articles/360042295174-What-is-the-license-of-the-photos-and-videos-on-Pexels)
- [Pexels — commercial use](https://help.pexels.com/hc/en-us/articles/360042295214-Can-I-use-the-photos-and-videos-for-a-commercial-project)
- [Pexels API guidelines](https://cat.pexels.com/api/)
