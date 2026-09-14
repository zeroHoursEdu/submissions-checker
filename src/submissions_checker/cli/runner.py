"""Standalone check runner — validate a subject config against the real Docker sandbox.

This is the artifact subject repos depend on (shipped as the `submissions-checker-runner`
Docker image). It loads a subject `config.yml`, runs checks through the same DB-free core
production uses (`services.check_core`), and asserts outcomes against expectations.

Commands
--------
    runner run --config config.yml --assignment lab1 --variant 3 --submission ./fix/correct
    runner run-suite tests/suite.yml

The CLI never needs a database, scheduler, or any app service. It drives the subject's own
baked sandbox image via the host Docker daemon, so checks run exactly as in production.
See docs/runner-contract.md for the stability contract (CLI flags + suite.yml schema).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from submissions_checker.services import check_core
from submissions_checker.services.subject_config import load_config

EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_USAGE = 2
EXIT_ERROR = 3


@dataclass
class CaseResult:
    label: str
    expect: str
    expect_score: int | None
    outcome: check_core.CheckOutcome
    ok: bool
    detail: str


def _resolve_root(suite_path: Path, root_override: str | None) -> Path:
    """Repo root for resolving config + fixtures. Defaults to suite's parent's parent.

    A suite at `<repo>/tests/suite.yml` → `<repo>`. Overridable via the suite's `root` key.
    """
    if root_override:
        return (suite_path.parent / root_override).resolve()
    return suite_path.parent.parent.resolve()


def _evaluate(
    *,
    label: str,
    config: dict[str, Any],
    assignment: str,
    variant: str | None,
    submission_dir: Path,
    plugin_dir: Path,
    expect: str,
    expect_score: int | None,
) -> CaseResult:
    outcome = asyncio.run(
        check_core.check_submission(
            config=config,
            assignment_code=assignment,
            variant=variant,
            submission_dir=submission_dir,
            plugin_dir=plugin_dir,
        )
    )

    if outcome.status == "config_error":
        return CaseResult(
            label, expect, expect_score, outcome, False, f"config error: {outcome.reason}"
        )

    if expect not in ("pass", "fail"):
        return CaseResult(
            label,
            expect,
            expect_score,
            outcome,
            False,
            f"invalid expect={expect!r} (use pass|fail)",
        )

    want_pass = expect == "pass"
    ok = outcome.passed == want_pass
    detail = f"status={outcome.status} score={outcome.score}/{outcome.max_score}"
    if ok and expect_score is not None and outcome.score != expect_score:
        ok = False
        detail += f" (expected score {expect_score})"
    return CaseResult(label, expect, expect_score, outcome, ok, detail)


def _print_human(results: list[CaseResult]) -> None:
    for r in results:
        mark = "PASS" if r.ok else "FAIL"
        print(f"[{mark}] {r.label}  expect={r.expect}  {r.detail}")
    passed = sum(1 for r in results if r.ok)
    print(f"\n{passed}/{len(results)} cases matched expectations.")


def _print_json(results: list[CaseResult]) -> None:
    payload = {
        "cases": [
            {
                "label": r.label,
                "expect": r.expect,
                "expect_score": r.expect_score,
                "ok": r.ok,
                "status": r.outcome.status,
                "score": r.outcome.score,
                "max_score": r.outcome.max_score,
                "reason": r.outcome.reason,
                "detail": r.detail,
            }
            for r in results
        ],
        "matched": sum(1 for r in results if r.ok),
        "total": len(results),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _cmd_run(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    plugin_dir = config_path.parent
    submission_dir = Path(args.submission).resolve()
    result = _evaluate(
        label=f"{args.assignment}"
        + (f"/v{args.variant}" if args.variant else "")
        + f" {submission_dir.name}",
        config=config,
        assignment=args.assignment,
        variant=args.variant,
        submission_dir=submission_dir,
        plugin_dir=plugin_dir,
        expect=args.expect,
        expect_score=args.expect_score,
    )
    results = [result]
    (_print_json if args.json else _print_human)(results)
    return EXIT_OK if result.ok else EXIT_MISMATCH


def _cmd_run_suite(args: argparse.Namespace) -> int:
    suite_path = Path(args.suite).resolve()
    if not suite_path.exists():
        print(f"error: suite file not found: {suite_path}", file=sys.stderr)
        return EXIT_USAGE

    suite = load_config(suite_path)  # same YAML parser
    if not isinstance(suite, dict) or "cases" not in suite:
        print("error: suite must be a mapping with a 'cases' list", file=sys.stderr)
        return EXIT_USAGE

    root = _resolve_root(suite_path, suite.get("root"))
    config_path = (root / suite.get("config", "config.yml")).resolve()
    if not config_path.exists():
        print(f"error: config not found: {config_path}", file=sys.stderr)
        return EXIT_USAGE
    config = load_config(config_path)
    plugin_dir = config_path.parent

    results: list[CaseResult] = []
    for i, case in enumerate(suite["cases"]):
        missing = [k for k in ("assignment", "submission", "expect") if k not in case]
        if missing:
            print(f"error: case #{i} missing required keys: {missing}", file=sys.stderr)
            return EXIT_USAGE
        variant = case.get("variant")
        variant = str(variant) if variant is not None else None
        submission_dir = (root / case["submission"]).resolve()
        label = case.get(
            "name",
            f"{case['assignment']}"
            + (f"/v{variant}" if variant else "")
            + f" {Path(case['submission']).name}",
        )
        if not submission_dir.exists():
            results.append(
                CaseResult(
                    label,
                    case["expect"],
                    case.get("expect_score"),
                    check_core.CheckOutcome("config_error", 0, 0, [], "fixture dir missing"),
                    False,
                    f"fixture not found: {submission_dir}",
                )
            )
            continue
        results.append(
            _evaluate(
                label=label,
                config=config,
                assignment=case["assignment"],
                variant=variant,
                submission_dir=submission_dir,
                plugin_dir=plugin_dir,
                expect=case["expect"],
                expect_score=case.get("expect_score"),
            )
        )

    (_print_json if args.json else _print_human)(results)
    return EXIT_OK if all(r.ok for r in results) else EXIT_MISMATCH


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="submissions-checker-runner",
        description="Validate a subject config against the real Docker check sandbox.",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run a single check against one submission directory")
    p_run.add_argument("--config", required=True, help="path to the subject config.yml")
    p_run.add_argument("--assignment", required=True, help="assignment code (e.g. lab1)")
    p_run.add_argument("--variant", default=None, help="variant id (optional)")
    p_run.add_argument("--submission", required=True, help="submission/fixture directory")
    p_run.add_argument(
        "--expect",
        default="pass",
        choices=("pass", "fail"),
        help="expected outcome (default: pass)",
    )
    p_run.add_argument("--expect-score", type=int, default=None, dest="expect_score")
    p_run.set_defaults(func=_cmd_run)

    p_suite = sub.add_parser("run-suite", help="run all cases declared in a suite.yml")
    p_suite.add_argument("suite", help="path to tests/suite.yml")
    p_suite.set_defaults(func=_cmd_run_suite)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except check_core.CheckExecutionError as exc:
        print(f"error: sandbox technical failure: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
