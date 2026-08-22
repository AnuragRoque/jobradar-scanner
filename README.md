# JobRadar

A local arrivals board for jobs at the companies you actually want. Polls each
company's applicant tracking system directly — no scraping, no login, no API keys,
no cloud. Stdlib Python only.

```
python3 jobradar.py serve
```

→ http://localhost:8765

## Why it works this way

Almost every company runs its careers page on a hosted ATS, and those ATSs expose
the same public JSON their own careers page consumes. JobRadar talks to that JSON
directly, so a posting shows up on your board within minutes of going live —
usually well before it propagates to Naukri or LinkedIn search.

Supported: **Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee, Workday.**
That covers most product companies and a good share of India MNC careers portals.
Naukri, Taleo and hand-rolled career pages are not supported — those still need the
Chrome/scout route.

## Setup

```bash
# 1. add your dream companies (each URL is verified against the live board first)
python3 jobradar.py add https://jobs.lever.co/somecompany
python3 jobradar.py add https://job-boards.greenhouse.io/somecompany --name "Some Co"

# or 20 at once
python3 jobradar.py bulk dream-companies.txt

# 2. first poll
python3 jobradar.py fetch

# 3. run it
python3 jobradar.py serve --every 10
```

Paste the URL you land on when you click "Careers" — JobRadar figures out which ATS
it is. If the page is custom-built, it fetches the HTML and looks for an embedded
board before giving up. You can also paste a URL straight into the field at the top
of the dashboard.

### Already have a company name list

If you have a plain list of names (your `companies.json` from the scout work, or a
JSON array of strings), the resolver tries slug patterns across all six hosted
platforms and keeps whatever actually returns postings:

```bash
python3 jobradar.py resolve /path/to/companies.json
```

Expect roughly a third to resolve automatically. The rest are on Workday, Taleo or
Naukri — add the Workday ones by URL, drop the others.

## Commands

|                                    |                                                         |
| ---------------------------------- | ------------------------------------------------------- |
| `add <url> [--name N]`           | detect the ATS, verify it responds, save it             |
| `bulk <file.txt>`                | one careers URL per line                                |
| `resolve <names.json>`           | bulk slug-guessing across six platforms                 |
| `fetch`                          | poll every source once, score, merge                    |
| `serve [--port N] [--every MIN]` | dashboard + background polling (default 10 min)         |
| `doctor`                         | re-check every configured source, show which are broken |
| `list`                           | show what's being tracked                               |

## The dashboard

Newest arrival at the top. An amber left edge and an amber age chip mean you haven't
opened it yet. Click any row to expand it.

- **Age** — time since the posting first landed on your board, not since it was written.
- **Fit** — 0–10 from `profile.json`. Ten segments so you can scan the column.
- **Matches your stack** — which of your keywords the JD actually contains.
- **Gaps to address** — keywords in the JD you don't currently claim. Feed these to
  the resume tailor, or treat them as the learning list.
- **Save / Applied / Hide** — persists to `data/store.json`. Anything you mark stays
  put even when the posting later disappears from the board.
- **Enable alerts** — browser notification the moment something new arrives, plus an
  unread count in the tab title. That's the "stop refreshing" part.

Filters: unread, last 24h, last 7d, fit 7+, remote, Delhi NCR, saved, applied.

## Tuning `profile.json`

This is the whole ranking model and it's meant to be edited.

- `titles_core` / `titles_ok` — big and moderate title bonuses.
- `titles_block` — postings whose title contains any of these are dropped outright.
  Currently drops manager/director/intern/architect/sales roles. Loosen if you want
  to see "Solutions Architect – AI".
- `skills` — keyword → points. Your Ollama / MCP / OCR / RAG terms carry the most.
- `gaps` — keyword → penalty, capped at 12 total so a strong role with one Kubernetes
  line still ranks. The names still show on the card either way.
- `locations` — remote and NCR weighted top; anything outside India (and not remote)
  is filtered out entirely by `location_required`.
- `years_max` — postings asking for more get flagged STRETCH and lose points.

Change a weight, run `fetch`, and every stored job is rescored.

## Files

```
jobradar.py            engine, adapters, scorer, server
profile.json           what "relevant" means — edit this
companies.json         the boards being polled
dream-companies.txt    paste-a-URL-per-line template for bulk
dashboard.html         the board — edit freely, it's served from disk
data/store.json        every job seen, with your statuses
```

The three rows on the board right now are labelled **demo row**. They vanish on your
first real `fetch`.

## Notes

- Polling every 10 minutes across 20 boards is about 120 requests an hour, which is
  well within what these public endpoints tolerate. Don't drop below 5.
- Nothing leaves your machine and nothing needs a key. The server binds to
  `127.0.0.1` only.
- To run it always-on, a `launchd`/Task Scheduler entry or a `tmux` session pointing
  at `serve` is enough — there's no daemon to install.
