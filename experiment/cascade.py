"""Compares single-model extraction with a cascade on the loan-dossier-intake evaluation set.

Strategies (classification always runs on the small model):
- single:<model>  every document is extracted by <model>
- cascade         every document is extracted by the small model; if the rules raise a finding that a misread
                  scan could cause, the scanned documents are extracted again by the large model and the rules
                  run again on the result

Model responses come from the response cache that loan-dossier-intake's evaluation fills (var/cache), so the
cascade sees exactly the answers each model gave in the single-model runs. By default the script never calls
the API and stops at the first request that is not cached.

    uv run python experiment/cascade.py                 # offline, from the cache
    uv run python experiment/cascade.py --allow-api     # fill missing responses from the API
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from pathlib import Path

from dotenv import find_dotenv, load_dotenv
from loan_intake.checks import decide, run_checks
from loan_intake.cli import VAR
from loan_intake.documents import load_dir
from loan_intake.evaluation.idp import FIXTURES, score_dossier, summarise
from loan_intake.extract import read_document
from loan_intake.llm import AnthropicClient, CachingClient
from loan_intake.policy import load_thresholds
from loan_intake.schemas import DossierResult, ExtractedDocument

SMALL, LARGE, LARGEST = "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"
RESULTS = Path(__file__).resolve().parents[1] / "results"

# Findings a misread on a scanned document can produce. Product-limit findings (DTI, amount, term) come from
# text documents and are not a reason to re-read anything.
SCAN_SENSITIVE = {"NAME_MISMATCH", "ID_MISMATCH", "DOB_MISMATCH", "NAME_DIACRITICS_DIFFER", "PAYSLIP_NAME_MISMATCH",
                  "ID_NUMBER_INCONSISTENT", "ID_EXPIRY_IMPLAUSIBLE", "ID_EXPIRED", "MISSING_FIELD", "FORMAT_INVALID",
                  "EXTRACTION_FAILED"}


class CacheOnly:
    def complete(self, **request):
        raise RuntimeError(f"response for {request['model']} / {request['step']} is not cached; run with --allow-api")


def dossier_result(name: str, docs: list[ExtractedDocument], as_of: date) -> DossierResult:
    findings, metrics = run_checks(docs, load_thresholds(), as_of)
    return DossierResult(name, decide(findings), docs, findings, metrics)


def single(folder: Path, client, model: str, as_of: date) -> tuple[DossierResult, bool]:
    docs = [read_document(src, client, model, SMALL) for src in load_dir(folder)]
    return dossier_result(folder.name, sorted(docs, key=lambda d: d.name), as_of), False


def cascade(folder: Path, client, as_of: date) -> tuple[DossierResult, bool]:
    sources = {s.name: s for s in load_dir(folder)}
    first, _ = single(folder, client, SMALL, as_of)
    if not ({f.code for f in first.findings} & SCAN_SENSITIVE) or not any(d.scanned for d in first.documents):
        return first, False
    docs = []
    for doc in first.documents:
        if not doc.scanned:
            docs.append(doc)
            continue
        again = read_document(sources[doc.name], client, LARGE, SMALL)
        # The classification is reused in practice, so only the large model's extraction call is added.
        docs.append(replace(again, usage=doc.usage + [u for u in again.usage if u.step == "extract"]))
    return dossier_result(folder.name, docs, as_of), True


def run(strategy: str, client, prefix: str = "") -> dict:
    folders = sorted(p for p in FIXTURES.iterdir() if p.is_dir() and p.name.startswith(prefix))

    def one(folder: Path) -> dict:
        expected = json.loads((folder / "expected.json").read_text(encoding="utf-8"))
        as_of = date.fromisoformat(expected["as_of"])
        if strategy == "cascade":
            result, escalated = cascade(folder, client, as_of)
        else:
            result, escalated = single(folder, client, strategy.split(":", 1)[1], as_of)
        return {**score_dossier(result, expected), "escalated": escalated}

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(one, folders))
    subsets = {"standard": [r for r in rows if r["dossier"].startswith("D")],
               "stress": [r for r in rows if r["dossier"].startswith("H")]}
    summary = {}
    for name, subset in subsets.items():
        if subset:
            summary[name] = {**summarise(subset), "escalated": sum(r["escalated"] for r in subset)}
    return {"strategy": strategy, "summary": summary, "rows": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-api", action="store_true")
    args = parser.parse_args(argv)
    load_dotenv(find_dotenv(usecwd=True))
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    client = CachingClient(AnthropicClient() if args.allow_api else CacheOnly(), VAR / "cache")
    runs = [run(f"single:{SMALL}", client), run(f"single:{LARGE}", client), run("cascade", client),
            run(f"single:{LARGEST}", client, prefix="H")]
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "runs.json").write_text(json.dumps(runs, ensure_ascii=False, indent=1), encoding="utf-8")
    for r in runs:
        for subset, s in r["summary"].items():
            print(f"{r['strategy']:28} {subset:8} decision {s['decision_accuracy_pct']}% "
                  f"false-review {s['false_review_rate_pct']}% wrong-data {s['ready_with_wrong_data']} "
                  f"escalated {s['escalated']} cost/dossier ${s['cost_per_dossier_usd']} p50 {s['latency_p50_s']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
