# Ashby Job Explorer

A self-built job search tool for aggregating and filtering postings across thousands of companies that use the Ashby ATS — built with AI-assisted development (Claude) end-to-end, from architecture decisions through debugging and testing.

**[Live demo](#)**

## What it does

- Aggregates open job postings from thousands of companies via Ashby's public posting API — no scraping, all real, documented API calls
- Two-stage discovery pipeline (included as standalone Python scripts) that finds and validates candidate companies against Ashby's live API before they're ever added to the tracked list
- Smart filtering: a location parser that collapses dozens of inconsistent free-text location strings ("New York, NY", "NYC", "US - New York NY") into one clean country → state/city hierarchy
- Compensation extraction that pulls salary data out of unstructured job descriptions when a company didn't fill in Ashby's structured pay field, with a confidence distinction between verified and inferred numbers
- A resume-paste feature that suggests saved search filters based on role/function keywords found in pasted resume text
- Company-card and job-card browsing views, saved keyword filters, refresh history tracking with day-over-day deltas
- Runs entirely client-side — no backend, no server costs, data persists locally in the browser (IndexedDB)

## Why I built it

I was job searching and got tired of the existing tools — most either cost a recurring fee, were cluttered with ads, or didn't let me search across enough companies at once. I decided to build my own, using Claude to pair-program the whole thing: architecture, feature planning, debugging real bugs (including a few I introduced myself along the way and caught through testing), performance work at scale (~3,000 companies, ~50,000 job postings), and an accessibility pass.

## The discovery pipeline

Two standalone Python scripts (no dependencies beyond the standard library) handle finding and validating new companies:

- `ashby_discovery.py` — validates a list of candidate company names against Ashby's real API, with resume-on-interrupt support so a long validation run never loses progress
- `ashby_commoncrawl_discovery.py` — searches Common Crawl's free public web index to discover new candidate companies, with retry-with-backoff for transient server errors

## Running it

The whole tool is a single self-contained HTML file — download `index.html` and open it in any browser. No install, no build step, no dependencies.

## Tech

Vanilla HTML/CSS/JS. No framework, no build tooling, no external runtime dependencies. Chosen deliberately to keep the tool something anyone could download and run immediately.
