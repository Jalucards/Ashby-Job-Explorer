#!/usr/bin/env python3
"""
Ashby Discovery — candidate validator

Takes a list of candidate company names (one per line, in candidates.txt
by default) and checks each one against Ashby's real public job-board API
to find a working slug. Outputs a JSON file in the exact shape the
Ashby Job Explorer's "Import list" button expects, with real provenance
data attached to each company.

No third-party packages required — uses only the Python standard library,
so there's nothing to pip install.

USAGE
  1. Put one candidate company name per line in candidates.txt, next to
     this script. Names can be plain ("Ramp") or full URLs
     ("https://jobs.ashbyhq.com/ramp") — both work.
  2. Run:  python3 ashby_discovery.py
  3. Open the Job Explorer, click "Import list", and select the output
     file this script writes (ashby_discovery_results.json).

RESUMING: results are saved after every single candidate, not just at the
end. If the script is interrupted (closed terminal, sleep, crash, etc.),
just run it again — it picks up where it left off instead of starting
over. To force a completely fresh run, delete ashby_discovery_progress.json
first.

RE-VALIDATING: companies you already found once can go stale — job counts
change, and companies sometimes leave Ashby entirely. Run:
    python3 ashby_discovery.py --revalidate
to re-check every already-resolved company's real slug directly (no
guessing needed, since the slug is already confirmed) and refresh its
job count, status, and lastValidatedAt. A company that stops resolving
gets marked "dead" rather than deleted, so you keep the history and can
decide what to do with it in the main tool.

RELEVANCE SCORING: every validated company also gets a relevantJobCount
and a short sample of matching titles, based on whether any of its open
jobs look like Sales/BD, Account Management/Customer Success, or
Operations/Support roles (see RELEVANT_KEYWORDS below \u2014 edit that list
any time your target roles change). This exists because most Ashby
companies skew heavily toward engineering hiring, so raw discovery volume
doesn't translate into relevant openings. Use it to decide which
newly-found companies are actually worth importing, instead of importing
everything and finding out later.

This script does NOT search the web for new company names — it only
validates candidates you already have. See the bottom of this file for
notes on why, and what a "find new names" pass would require.
"""

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

INPUT_FILE = "candidates.txt"
OUTPUT_FILE = "ashby_discovery_results.json"
UNRESOLVED_FILE = "ashby_discovery_unresolved.txt"
PROGRESS_FILE = "ashby_discovery_progress.json"

# Be polite to Ashby's API — this is a personal tool, not a scraper.
REQUEST_DELAY_SECONDS = 0.6
REQUEST_TIMEOUT_SECONDS = 10
USER_AGENT = "ashby-job-explorer-discovery/1.0 (personal use)"

# Checked against each open job's title AND department/team fields (lowercased,
# substring match) to flag companies actually worth adding — not just ones
# that happen to have a working Ashby board. Edit this list any time your
# target roles change; it only affects scoring, never which slugs validate.
RELEVANT_KEYWORDS = [
    # Sales / Business Development
    "sales", "business development", "bdr", "sdr", "account executive",
    # Account Management / Customer Success
    "account management", "account manager", "customer success", "client success",
    "csm", "customer experience",
    # Operations / Support
    "operations", "customer support", "client support", " ops", "ops ",
]


def job_is_relevant(job):
    haystack = " ".join(str(job.get(f, "")) for f in ("title", "department", "team")).lower()
    return any(kw in haystack for kw in RELEVANT_KEYWORDS)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def slug_from_url(value):
    """If the candidate is a full jobs.ashbyhq.com URL, pull the slug out."""
    try:
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme in ("http", "https") and parsed.netloc.lower() == "jobs.ashbyhq.com":
            part = [p for p in parsed.path.split("/") if p]
            if part:
                return urllib.parse.unquote(part[0])
    except Exception:
        pass
    return None


def candidate_variants(raw):
    """
    Generate a small set of plausible slug variants for a company name,
    since we don't know Ashby's exact casing/formatting in advance.
    Ashby board names appear to be matched case-insensitively in practice,
    so casing variants mostly guard against unusual formatting rather than
    case itself — but we keep a few forms to be safe.
    """
    raw = raw.strip().rstrip(",")
    if not raw:
        return []

    url_slug = slug_from_url(raw)
    if url_slug:
        return [url_slug]

    variants = []
    seen = set()

    def add(v):
        v = v.strip()
        if v and v not in seen:
            seen.add(v)
            variants.append(v)

    add(raw)                                    # as-given, e.g. "Mistral AI"
    add(raw.replace(" ", ""))                    # "MistralAI"
    add(raw.replace(" ", "-"))                    # "Mistral-AI"
    add(raw.lower().replace(" ", ""))             # "mistralai"
    add(raw.lower().replace(" ", "-"))            # "mistral-ai"

    return variants


def check_slug(slug):
    """
    Calls Ashby's public posting API for one slug. Returns a dict describing
    what happened — never raises, so a bad slug just gets marked invalid.
    """
    url = (
        "https://api.ashbyhq.com/posting-api/job-board/"
        + urllib.parse.quote(slug)
        + "?includeCompensation=false"
    )
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            if resp.status != 200:
                return {"ok": False, "status": f"http-{resp.status}"}
            data = json.loads(resp.read().decode("utf-8"))
            jobs = data.get("jobs", []) if isinstance(data, dict) else None
            if not isinstance(jobs, list):
                return {"ok": False, "status": "invalid-response"}
            open_jobs = [j for j in jobs if isinstance(j, dict) and j.get("isListed") is not False]
            relevant_jobs = [j for j in open_jobs if job_is_relevant(j)]
            return {
                "ok": True,
                "status": "active" if open_jobs else "valid-empty",
                "jobCount": len(open_jobs),
                "relevantJobCount": len(relevant_jobs),
                # A short sample so you can sanity-check the match at a glance
                # without opening the board yourself.
                "relevantTitles": [j.get("title", "") for j in relevant_jobs[:3]],
            }
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": f"http-{e.code}"}
    except urllib.error.URLError as e:
        return {"ok": False, "status": f"network-error: {e.reason}"}
    except Exception as e:
        return {"ok": False, "status": f"error: {e}"}


def load_candidates(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f]
    except FileNotFoundError:
        print(f"Couldn't find {path}. Create it with one company name per line, then re-run.")
        sys.exit(1)
    # drop blanks and comment lines
    return [line for line in lines if line and not line.startswith("#")]


def load_progress():
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        resolved = data.get("resolved", [])
        unresolved = data.get("unresolved", [])
        # Older runs stored unresolved as a plain list of name strings, which
        # threw away *why* each one failed. Upgrade those in place so
        # re-running an old progress file doesn't lose anything going forward.
        unresolved = [
            {"name": u, "lastError": "unknown (from an earlier version of this script)", "checkedAt": None}
            if isinstance(u, str) else u
            for u in unresolved
        ]
        return resolved, unresolved
    except FileNotFoundError:
        return [], []
    except (json.JSONDecodeError, AttributeError):
        print(f"Warning: {PROGRESS_FILE} exists but couldn't be read \u2014 starting fresh.")
        return [], []


def save_progress(resolved, unresolved):
    """Writes progress AND the user-facing output files after every candidate,
    so an interruption at any point still leaves usable, importable results."""
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump({"resolved": resolved, "unresolved": unresolved}, f, indent=2)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump({"companies": resolved}, f, indent=2)
    with open(UNRESOLVED_FILE, "w", encoding="utf-8") as f:
        # Human-scannable: name plus the reason it failed, one per line.
        f.write("\n".join(f"{u['name']}  ({u['lastError']})" for u in unresolved))


def run_discovery():
    candidates = load_candidates(INPUT_FILE)
    if not candidates:
        print(f"{INPUT_FILE} is empty. Add some company names (one per line) and re-run.")
        sys.exit(1)

    resolved, unresolved = load_progress()
    already_done = (
        set(c["candidateName"].lower() for c in resolved)
        | set(u["name"].lower() for u in unresolved)
    )

    remaining = [c for c in candidates if c.lower() not in already_done]

    if already_done:
        print(f"Resuming from a previous run: {len(already_done)} candidates already checked, "
              f"{len(remaining)} remaining.\n"
              f"(Delete {PROGRESS_FILE} first if you want a completely fresh run.)\n")

    if not remaining:
        print("Nothing left to check \u2014 every candidate in this file was already processed.")
        print(f"  Verified companies -> {OUTPUT_FILE}")
        if unresolved:
            print(f"  Unresolved names   -> {UNRESOLVED_FILE}")
        print(f"\nTip: to catch companies that changed since you found them (new jobs, or "
              f"companies that left Ashby), run:  python3 {sys.argv[0]} --revalidate")
        return

    print(f"Checking {len(remaining)} candidate{'s' if len(remaining) != 1 else ''}...\n")
    total = len(candidates)

    for i, name in enumerate(remaining, 1):
        found = None
        last_failure = None
        for variant in candidate_variants(name):
            result = check_slug(variant)
            time.sleep(REQUEST_DELAY_SECONDS)
            if result["ok"]:
                found = {"slug": variant, **result}
                break
            last_failure = result["status"]

        timestamp = now_iso()
        progress_position = total - len(remaining) + i

        if found:
            relevance_note = (f", {found['relevantJobCount']} look relevant"
                               if found['relevantJobCount'] > 0 else ", none look relevant")
            print(f"  [{progress_position}/{total}] OK    {name!r} -> slug '{found['slug']}' "
                  f"({found['status']}, {found['jobCount']} open jobs{relevance_note})")
            resolved.append({
                "slug": found["slug"],
                "enabled": True,
                "source": "discovery-script",
                "candidateName": name,
                "addedAt": timestamp,
                "lastValidatedAt": timestamp,
                "verificationStatus": found["status"],
                "jobCount": found["jobCount"],
                "relevantJobCount": found["relevantJobCount"],
                "relevantTitles": found["relevantTitles"],
                "consecutiveErrors": 0,
            })
        else:
            print(f"  [{progress_position}/{total}] FAIL  {name!r} \u2014 no working slug found "
                  f"(last attempt: {last_failure})")
            unresolved.append({
                "name": name,
                "lastError": last_failure or "no variants attempted",
                "checkedAt": timestamp,
            })

        # Save after every single candidate, not just at the end.
        save_progress(resolved, unresolved)

    relevant_count = sum(1 for c in resolved if c.get("relevantJobCount", 0) > 0)
    print(f"\nDone. {len(resolved)} verified, {len(unresolved)} unresolved.")
    print(f"  Of the {len(resolved)} verified companies, {relevant_count} have at least one "
          f"job that looks relevant to your target departments.")
    print(f"  Verified companies -> {OUTPUT_FILE}  (import this into the Job Explorer)")
    if unresolved:
        print(f"  Unresolved names   -> {UNRESOLVED_FILE}  (check these manually \u2014 the real "
              f"slug may not match any of the guessed variants)")


def run_revalidate():
    """
    Re-checks every already-resolved company directly by its confirmed slug
    (no variant guessing needed — we already know the real one). Updates
    jobCount/status/lastValidatedAt in place. A company that stops resolving
    is marked "dead" rather than removed, so the history is preserved and
    it's your call what to do with it in the main tool (Clear Failed, etc).
    """
    resolved, unresolved = load_progress()
    if not resolved:
        print("Nothing to revalidate yet \u2014 run the script without --revalidate first "
              "to build up a list of verified companies.")
        return

    print(f"Revalidating {len(resolved)} previously-found companies...\n")
    changed = 0
    went_dead = 0
    came_back = 0

    for i, company in enumerate(resolved, 1):
        old_status = company.get("verificationStatus")
        old_job_count = company.get("jobCount", 0)
        result = check_slug(company["slug"])
        time.sleep(REQUEST_DELAY_SECONDS)
        timestamp = now_iso()
        company["lastValidatedAt"] = timestamp

        if result["ok"]:
            company["verificationStatus"] = result["status"]
            company["jobCount"] = result["jobCount"]
            company["relevantJobCount"] = result["relevantJobCount"]
            company["relevantTitles"] = result["relevantTitles"]
            company["consecutiveErrors"] = 0
            if old_status == "dead":
                came_back += 1
                print(f"  [{i}/{len(resolved)}] BACK  '{company['slug']}' is resolving again "
                      f"({result['jobCount']} open jobs)")
            elif old_job_count != result["jobCount"] or old_status != result["status"]:
                changed += 1
                print(f"  [{i}/{len(resolved)}] CHANGED '{company['slug']}': "
                      f"{old_job_count} -> {result['jobCount']} jobs "
                      f"({old_status} -> {result['status']})")
            else:
                print(f"  [{i}/{len(resolved)}] same  '{company['slug']}' "
                      f"({result['jobCount']} open jobs, unchanged)")
        else:
            company["consecutiveErrors"] = company.get("consecutiveErrors", 0) + 1
            # Only mark it dead after a couple of consecutive failures, since
            # a single timeout or transient 5xx doesn't mean the company left
            # Ashby — it might just mean Ashby's API had a bad moment.
            if company["consecutiveErrors"] >= 2:
                if old_status != "dead":
                    went_dead += 1
                    print(f"  [{i}/{len(resolved)}] DEAD  '{company['slug']}' has failed "
                          f"{company['consecutiveErrors']} checks in a row ({result['status']})")
                company["verificationStatus"] = "dead"
            else:
                print(f"  [{i}/{len(resolved)}] warn  '{company['slug']}' failed this check "
                      f"({result['status']}) \u2014 will confirm dead if it fails again next run")

        save_progress(resolved, unresolved)

    relevant_count = sum(1 for c in resolved if c.get("relevantJobCount", 0) > 0)
    print(f"\nRevalidation done. {changed} changed, {went_dead} newly marked dead, "
          f"{came_back} came back to life.")
    print(f"  {relevant_count} of {len(resolved)} tracked companies currently have at least "
          f"one job that looks relevant to your target departments.")
    print(f"  Updated companies -> {OUTPUT_FILE}  (re-import into the Job Explorer to pick up changes)")


def main():
    if "--revalidate" in sys.argv:
        run_revalidate()
    else:
        run_discovery()


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# Note on "finding new company names" (as opposed to validating known ones):
#
# This script deliberately only validates candidates you already have. Doing
# real discovery — searching the web, crawling Common Crawl indexes, or
# scanning company career pages for embedded Ashby widgets — needs different,
# heavier machinery (a search API or crawler, rate-limit handling, storage
# for a much larger candidate pool) and runs a real risk of getting your IP
# rate-limited or blocked by search engines if done carelessly.
#
# If you want that built later, it's a genuinely separate script from this
# one, not an extension of it — worth treating as its own project rather
# than bolting onto this validator.
# ---------------------------------------------------------------------------
