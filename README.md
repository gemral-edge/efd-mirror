# efd-mirror

A small fetch-only mirror of public U.S. Senate Periodic Transaction Reports (PTR) published on [efdsearch.senate.gov](https://efdsearch.senate.gov).
A scheduled GitHub Actions workflow downloads the public filings politely (rate-limited, with retry/backoff) and stores the raw JSON as a short-lived workflow artifact.
No data is committed to this repository.
