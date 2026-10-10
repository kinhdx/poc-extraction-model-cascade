# PoC: rule-triggered model cascade for loan document extraction

| | |
|---|---|
| Question | Can a small model read every document, with a larger model re-reading scanned documents only when the rules flag a possible misread, match the larger model's decisions at a lower cost? |
| Time box | One week |
| Status | Concluded, 10/10/2026 |
| Decision | **Go, as a trial** in back-office intake, with the conditions below |
| System under test | [loan-dossier-intake](https://github.com/kinhdx/loan-dossier-intake) at commit `7460dad` (model responses recorded at `dc6b2b4`; prompts and documents are unchanged between the two) |

## Why this question

The intake evaluation showed two things. On text documents and clean scans, `claude-haiku-5-5` was as accurate
as `claude-sonnet-5-5` at about a fourteenth of the cost per dossier. On degraded ID card scans, Sonnet read more
fields correctly (92.9% against 83.6%) and sent fewer clean dossiers to review. A misread scan almost always
contradicts another document or a CCCD rule, so the rules already say when a second reading is worth paying
for.

## Options compared

| Option | Description |
|---|---|
| A | Haiku extracts every document |
| B | Sonnet extracts every document |
| C | Opus extracts every document (stress set only) |
| D | Cascade: Haiku extracts every document; if a finding that a misread scan can cause is raised (name, ID number or date of birth mismatch, CCCD number or expiry rules, missing or unparseable field), Sonnet re-reads the scanned documents and the rules run again |

Product-limit findings (debt-to-income, amount, term) come from text documents and never trigger a re-read.
Classification runs on Haiku in every option.

## Method

- Dataset: the 40 synthetic dossiers in loan-dossier-intake (30 standard, 10 stress with blurred 100 dpi ID
  scans, corrected amounts and payslips with the year-to-date column first).
- The cascade is computed from the same cached model responses as the single-model runs, so each option sees
  exactly the answers each model gave; differences come from the strategy, not from sampling noise. Running
  this repository makes no API calls unless `--allow-api` is passed.
- Cost is the sum of model calls per dossier. Latency is model time on the critical path; in the cascade, the
  re-read runs after the first pass, so it adds to the latency of escalated dossiers.

## Results

| | A: Haiku | B: Sonnet | C: Opus | D: Cascade |
|---|---|---|---|---|
| **Standard set (30)** | | | | |
| Decision accuracy | 100% | 100% | not run | 100% |
| Dossiers escalated | - | - | - | 10 |
| Cost per dossier | $0.0013 | $0.0185 | | $0.0042 |
| Latency p50 / p95 | 4.2 s / 6.6 s | 4.5 s / 8.2 s | | 4.7 s / 10.4 s |
| **Stress set (10)** | | | | |
| Decision accuracy | 50% | 70% | 70% | 70% |
| Clean dossiers sent to review (of 5) | 100% | 60% | 60% | 60% |
| Scanned field accuracy | 83.6% | 92.9% | 92.1% | 92.9% |
| Dossiers escalated | - | - | - | 10 |
| Cost per dossier | $0.0024 | $0.0240 | $0.0481 | $0.0206 |
| Latency p50 / p95 | 8.5 s / 9.0 s | 7.6 s / 8.6 s | 10.0 s / 11.6 s | 13.0 s / 13.9 s |
| **All 40** | | | | |
| Cost per dossier | $0.0015 | $0.0199 | | $0.0083 |
| Passed as ready with a misread field | 0 | 0 | 0 (stress) | 0 |

Raw results: [results/runs.json](results/runs.json).

## Findings

1. The cascade reached the same decision as Sonnet on all 40 dossiers, at 42% of Sonnet's cost across the set
   ($0.0083 against $0.0199 per dossier).
2. The saving depends on how many dossiers escalate. On the standard set, the 10 escalations were all dossiers
   with a planted identity defect; the re-read confirmed the defect, which is the right outcome but costs a
   second call. On the stress set every dossier escalated and the cascade cost only 14% less than Sonnet alone.
3. Escalation adds latency: about 5 seconds at p50 on the stress set. For back-office intake, where a reviewer
   picks the dossier up minutes or hours later, this does not matter. For an in-app flow with the customer
   waiting, it might.
4. Opus gave no improvement over Sonnet on the stress set at twice the cost, so it is not a candidate for the
   second stage.
5. No option passed a dossier with a misread field. That property comes from the rules, and it is what makes a
   cheap first pass acceptable.

## Decision

Go, as a trial, under these conditions:

- Back-office intake only, where the added latency is not seen by the customer.
- Measure the escalation rate on real traffic for the first month. The cascade stays cheaper than Sonnet alone
  even when every dossier escalates, because text documents are never re-read; but the saving falls from 77%
  to about 14% as escalation approaches 100%, and at that point the extra latency and moving parts may not be
  worth it.
- Keep the evaluation set as the gate for any change to the escalation trigger, and add real misreads to the
  stress set as they are found.

## Tech Radar entry

| Blip | Ring | Note |
|---|---|---|
| Rule-triggered model cascade for document extraction | Trial | Small model first; re-read scanned documents with a larger model only when deterministic checks flag a contradiction. Requires a rule layer that catches misreads. |
| Largest model for document extraction | Hold | No gain over the mid-size model on degraded scans in this evaluation, at twice the cost. |

## Not covered

- Prompt caching: the shared prefix of an extraction request (instructions and schema) is short, and the
  document itself changes on every call, so caching was left for a separate measurement.
- Real scans. Every number above comes from synthetic documents and is an upper bound.
- Throughput and rate limits under load.

## Reproduce

Clone this repository next to `loan-dossier-intake` (the pipeline and rules are imported from it), then:

```bash
uv sync
uv run python experiment/cascade.py     # offline, from cache/; reproduces results/runs.json at no cost
```

`cache/` holds the structured answers each model returned for the synthetic documents (values and quoted
spans, no document images), keyed by a hash of the request. Any change to the prompts, schemas or documents
in loan-dossier-intake changes the hashes; the script then stops and names the missing request, and
`--allow-api` refills the cache.
