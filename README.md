# NyayaDesk Supreme Court Extractor

An extraction tool for the public data published by [`vanga/indian-supreme-court-judgments`](https://github.com/vanga/indian-supreme-court-judgments).

**Live app:** [nyayadesk-supreme-court-extractor.vercel.app](https://nyayadesk-supreme-court-extractor.vercel.app)

The GitHub repository contains the collection code. The bulk data is stored in its public AWS S3 bucket. This tool reads the structured yearly Parquet metadata and optionally downloads matching English judgment PDFs.

## Live and local modes

The live Vercel app creates CSV or JSONL downloads directly in the request. To fit serverless response and execution limits, it accepts up to five years and 1,000 rows per export; `raw_html` exports are capped at 100 rows. It does not retain generated exports.

Use the local version for unrestricted multi-year jobs, SQLite output, persistent caching, and full English PDF text extraction.

## Start the visual tool

On macOS, double-click `start.command` or run:

```bash
./run.sh --open
```

The first run creates a private Python environment and installs DuckDB and pypdf. The tool then opens at:

```text
http://127.0.0.1:4181
```

Exports are saved in the `exports/` folder. Downloaded metadata and PDFs are cached in `.cache/`, so repeating an extraction does not download the same files again.

## Recommended first extraction

1. Enter `2023` in **Years**.
2. Leave the maximum at `100`.
3. Keep **Recommended columns** selected.
4. Keep PDF text turned off for the first test.
5. Choose CSV and click **Start extraction**.

Metadata for 2023 is roughly 5.4 MB. Downloading the full English PDF archive would be roughly 382 MB, but the tool downloads individual PDFs only when full-text extraction is enabled and only for matching rows.

## Command-line use

After running `./run.sh` once:

```bash
.venv/bin/python extractor.py \
  --years 2023 \
  --format csv \
  --output exports/2023-privacy.csv \
  --query privacy \
  --limit 100
```

For several years:

```bash
.venv/bin/python extractor.py \
  --years 2020-2024 \
  --format sqlite \
  --output exports/recent-cases.sqlite \
  --judge "D Y Chandrachud"
```

Add `--include-pdf-text` only when full judgment text is required.

## Output schema

The tool supports three kinds of columns:

- **Source columns:** Published metadata such as title, parties, citation, date, judges, disposal nature, language, source path and raw HTML.
- **Derived columns:** Mechanical fields such as source URLs, normalized judge names, bench strength, extraction timestamp, text hash and word count.
- **Analysis-ready columns:** Headnote, facts, issues, reasoning, holding, ratio, statutes, cited cases and later treatment.

Analysis-ready columns are deliberately blank. Metadata alone cannot reliably establish a ratio, legal holding or later treatment. A separate paragraph-linked enrichment and review pipeline should populate them.

`source_headnote` copies the dataset’s published description when available. `ai_headnote` remains blank unless an audited enrichment process creates one.

## Formats

- **CSV:** Best for Excel and manual inspection. UTF-8 with a BOM for compatibility.
- **JSONL:** Best for search indexing, embeddings and processing pipelines.
- **SQLite:** Best for large local datasets and SQL queries. The table is named `judgments`.

## Accuracy and provenance

Every row includes fields for the dataset source, repository, licence, source PDF URL, metadata URL and extraction time. If PDF extraction is enabled, it also records a SHA-256 hash and any extraction error.

The upstream dataset is CC BY 4.0. Preserve attribution when redistributing extracts. Court records should still be checked against the official judgment before legal use.
