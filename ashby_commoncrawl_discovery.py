#!/usr/bin/env python3
"""
Ashby Common Crawl Discovery — finds NEW candidate companies

Common Crawl is a free, public archive of billions of previously-crawled
web pages, queryable over plain HTTP — no browser, no scraping, nothing
against anyone's Terms of Service. This script asks it: "which URLs have
you ever indexed under jobs.ashbyhq.com?" and pulls out every distinct
company slug mentioned.

IMPORTANT: this only *discovers candidates* — it does NOT confirm they're
still live. Common Crawl's index can be months old, and companies rename
boards, get acquired, or stop using Ashby. Always run the output through
ashby_discovery.py afterward to validate each one against Ashby's real API
before importing anything into the Job Explorer.

USAGE
  python3 ashby_commoncrawl_discovery.py

  Optional: put a file called known_slugs.txt next to this script (one
  slug per line — e.g. export your current candidates.txt to this name)
  and it will skip anything you're already tracking, so the output is
  just the NEW finds.

OUTPUT
  new_candidates.txt — one newly-discovered company name per line, for
  reference / re-runs.

  New finds are ALSO appended directly into candidates.txt — the exact
  file ashby_discovery.py reads — so there's no manual copy step between
  the two scripts anymore. Just run ashby_discovery.py next.
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

KNOWN_SLUGS_FILE = "known_slugs.txt"
OUTPUT_FILE = "new_candidates.txt"
CANDIDATES_FILE = "candidates.txt"  # the file ashby_discovery.py reads

# How many of the most recent monthly Common Crawl indexes to search.
# More = more thorough but slower. 6 covers roughly the last half-year
# of crawls, which is a reasonable starting point.
NUM_CRAWLS_TO_SEARCH = 6

REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 20
USER_AGENT = "ashby-job-explorer-discovery/1.0 (personal research use)"
TARGET_URL_PATTERN = "jobs.ashbyhq.com/*"

MAX_PAGES_PER_CRAWL = 20  # safety cap so one huge crawl can't run forever


def get_recent_crawl_ids(n):
    """Common Crawl publishes a list of all its monthly index collections."""
    url = "https://index.commoncrawl.org/collinfo.json"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            collections = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"Couldn't fetch the list of Common Crawl indexes: {e}")
        sys.exit(1)
    # collinfo.json is newest-first already, but sort defensively just in case.
    ids = [c["id"] for c in collections if "id" in c]
    return ids[:n]


def slug_from_url(raw_url):
    try:
        parsed = urllib.parse.urlparse(raw_url)
        if parsed.netloc.lower() != "jobs.ashbyhq.com":
            return None
        part = [p for p in parsed.path.split("/") if p]
        if not part:
            return None
        return urllib.parse.unquote(part[0])
    except Exception:
        return None


RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = [5, 15, 30]  # wait times between attempts


def fetch_with_retry(url, label):
    """Fetches a URL, retrying on transient server errors (502/503/504) with
    backoff. Returns the response bytes, or None if all attempts failed."""
    last_error = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code == 404:
                raise  # not transient, caller should handle this specifically
            if e.code not in (502, 503, 504) or attempt == RETRY_ATTEMPTS:
                raise
        except Exception as e:
            last_error = e
            if attempt == RETRY_ATTEMPTS:
                raise

        wait = RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
        print(f"  [{label}] transient error ({last_error}), retrying in {wait}s "
              f"(attempt {attempt}/{RETRY_ATTEMPTS})...")
        time.sleep(wait)
    return None


def search_crawl(crawl_id):
    """Queries one Common Crawl index for jobs.ashbyhq.com URLs, paginated."""
    base = f"https://index.commoncrawl.org/{crawl_id}-index"
    encoded_pattern = urllib.parse.quote(TARGET_URL_PATTERN, safe="")

    # First, ask how many result pages exist for this query.
    count_url = f"{base}?url={encoded_pattern}&output=json&showNumPages=true"
    try:
        raw = fetch_with_retry(count_url, crawl_id)
        meta = json.loads(raw.decode("utf-8"))
        total_pages = min(meta.get("pages", 1), MAX_PAGES_PER_CRAWL)
    except Exception as e:
        print(f"  [{crawl_id}] couldn't get page count after retries ({e}), skipping")
        return set()

    found = set()
    for page in range(total_pages):
        page_url = f"{base}?url={encoded_pattern}&output=json&page={page}"
        try:
            raw = fetch_with_retry(page_url, f"{crawl_id} page {page}")
            records = []
            for line in raw.decode("utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except urllib.error.HTTPError as e:
            if e.code == 404:
                break  # ran out of pages
            print(f"  [{crawl_id}] page {page}: HTTP {e.code} after retries, stopping this crawl")
            break
        except Exception as e:
            print(f"  [{crawl_id}] page {page}: {e} after retries, stopping this crawl")
            break

        for record in records:
            slug = slug_from_url(record.get("url", ""))
            if slug:
                found.add(slug)

        print(f"  [{crawl_id}] page {page + 1}/{total_pages} \u2014 {len(found)} unique slugs so far")
        time.sleep(REQUEST_DELAY_SECONDS)

    return found


def load_known_slugs():
    try:
        with open(KNOWN_SLUGS_FILE, "r", encoding="utf-8") as f:
            return set(line.strip().lower() for line in f if line.strip())
    except FileNotFoundError:
        return set()


def append_to_candidates_file(new_slugs):
    """
    Feeds new finds straight into candidates.txt \u2014 the exact file
    ashby_discovery.py reads \u2014 so the next step is just running that
    script, with no manual copy/rename in between. Existing lines (and
    comments) in candidates.txt are preserved; only genuinely new slugs
    are appended, checked case-insensitively against what's already there.
    """
    try:
        with open(CANDIDATES_FILE, "r", encoding="utf-8") as f:
            existing_lines = [line.rstrip("\n") for line in f]
    except FileNotFoundError:
        existing_lines = []

    existing_lower = set(line.strip().lower() for line in existing_lines if line.strip() and not line.strip().startswith("#"))
    to_add = [s for s in new_slugs if s.lower() not in existing_lower]

    if not to_add:
        return 0

    with open(CANDIDATES_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(existing_lines))
        if existing_lines and existing_lines[-1].strip():
            f.write("\n")
        f.write(f"# --- {len(to_add)} added by ashby_commoncrawl_discovery.py on {datetime.now(timezone.utc).isoformat()} ---\n")
        f.write("\n".join(to_add))
        f.write("\n")

    return len(to_add)


def main():
    known = load_known_slugs()
    if known:
        print(f"Loaded {len(known)} already-known slugs from {KNOWN_SLUGS_FILE} \u2014 these will be excluded.\n")
    else:
        print(f"No {KNOWN_SLUGS_FILE} found \u2014 output will include everything found (nothing excluded).\n")

    print("Fetching the list of Common Crawl indexes...")
    crawl_ids = get_recent_crawl_ids(NUM_CRAWLS_TO_SEARCH)
    print(f"Searching the {len(crawl_ids)} most recent indexes: {', '.join(crawl_ids)}\n")

    all_found = set()
    for crawl_id in crawl_ids:
        print(f"Searching {crawl_id}...")
        found = search_crawl(crawl_id)
        all_found |= found

    new_slugs = sorted(s for s in all_found if s.lower() not in known)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(new_slugs))

    added_count = append_to_candidates_file(new_slugs)

    print(f"\nDone. Found {len(all_found)} total distinct slugs across all crawls searched.")
    print(f"{len(new_slugs)} are new (not in {KNOWN_SLUGS_FILE}) \u2014 written to {OUTPUT_FILE}.")
    if added_count > 0:
        print(f"{added_count} of those were appended straight into {CANDIDATES_FILE} \u2014 "
              f"no manual copy step needed.")
        print(f"\nNext step: run  python3 ashby_discovery.py  to validate them against "
              f"Ashby's real API and score them for relevance before importing anything.")
    else:
        print(f"Nothing new to add to {CANDIDATES_FILE} \u2014 everything found was already there.")


if __name__ == "__main__":
    main()
