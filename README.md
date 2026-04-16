# netdata-testdata

External test fixtures, data samples, and large reference datasets for the [Netdata](https://github.com/netdata/netdata) project.

## Why this repo exists

Some test fixtures are too large to keep in the main Netdata repository. This repo holds datasets that are valuable for testing but would otherwise bloat the main repo with millions of lines of non-code data.

There is no strict rule for what goes here vs. the main repo. The general guideline: if a dataset is large enough to meaningfully impact clone times, diff readability, or PR review quality, it belongs here.

## Attribution

Datasets in this repo may originate from third-party open-source projects. Each directory containing third-party data includes its own `ATTRIBUTION.md` and/or `NOTICE.md` with source, license, and provenance details.
