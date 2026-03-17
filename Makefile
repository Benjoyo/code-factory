.PHONY: setup lint format-check typecheck test test-coverage coverage-gate coverage-packages fix repair verify-static verify clean

UV := uv run --project . --extra dev --group dev
SOURCE_DIR := src/symphony
COVERAGE_JSON := coverage.json
LINE_COVERAGE_MIN := 100
BRANCH_COVERAGE_MIN := 100

setup:
	uv sync --extra dev --group dev

lint:
	@$(UV) ruff check --output-format concise .

format-check:
	@$(UV) ruff format --check .

typecheck:
	@$(UV) pyright

test:
	@$(UV) pytest -q

test-coverage:
	@$(UV) pytest -q --cov=$(SOURCE_DIR) --cov-branch --cov-report= --cov-report=json:$(COVERAGE_JSON)
	@$(UV) python -c '\
import json; \
from pathlib import Path; \
totals = json.loads(Path("$(COVERAGE_JSON)").read_text())["totals"]; \
line_actual = float(totals["percent_statements_covered"]); \
line_display = totals["percent_statements_covered_display"]; \
branches = totals["num_branches"]; \
covered_branches = totals["covered_branches"]; \
branch_percent = float(totals["percent_branches_covered"]); \
branch_display = totals["percent_branches_covered_display"]; \
print(f"Coverage: lines {line_display}% ({line_actual:.2f}%), branches {branch_display}% ({branch_percent:.2f}%, {covered_branches}/{branches})") \
'

coverage-gate: test-coverage
	@$(UV) python -c '\
import json; \
from pathlib import Path; \
line_minimum = float("$(LINE_COVERAGE_MIN)"); \
branch_minimum = float("$(BRANCH_COVERAGE_MIN)"); \
totals = json.loads(Path("$(COVERAGE_JSON)").read_text())["totals"]; \
line_actual = float(totals["percent_statements_covered"]); \
branch_actual = float(totals["percent_branches_covered"]); \
assert line_actual >= line_minimum, f"Line coverage gate failed: {line_actual:.2f}% < {line_minimum:.2f}%"; \
assert branch_actual >= branch_minimum, f"Branch coverage gate failed: {branch_actual:.2f}% < {branch_minimum:.2f}%" \
'

coverage-packages: test-coverage
	@printf '%s\n' \
	'import json' \
	'from collections import defaultdict' \
	'from pathlib import Path' \
	'data = json.loads(Path("$(COVERAGE_JSON)").read_text())' \
	'totals = data["totals"]' \
	'overall_lines = float(totals["percent_statements_covered"])' \
	'overall_lines_covered = totals["covered_lines"]' \
	'overall_lines_total = totals["num_statements"]' \
	'overall_branches = float(totals["percent_branches_covered"])' \
	'overall_branches_covered = totals["covered_branches"]' \
	'overall_branches_total = totals["num_branches"]' \
	'acc = defaultdict(lambda: {"covered_lines": 0, "num_statements": 0, "covered_branches": 0, "num_branches": 0})' \
	'for path, meta in data["files"].items():' \
	'    parts = Path(path).parts' \
	'    idx = parts.index("symphony")' \
	'    package = parts[idx + 1] if len(parts) > idx + 2 else "(root)"' \
	'    summary = meta["summary"]' \
	'    acc[package]["covered_lines"] += summary["covered_lines"]' \
	'    acc[package]["num_statements"] += summary["num_statements"]' \
	'    acc[package]["covered_branches"] += summary["covered_branches"]' \
	'    acc[package]["num_branches"] += summary["num_branches"]' \
	'print(f"overall\tlines {overall_lines:.2f}% ({overall_lines_covered}/{overall_lines_total})\tbranches {overall_branches:.2f}% ({overall_branches_covered}/{overall_branches_total})")' \
	'for package in sorted(acc):' \
	'    covered_lines = acc[package]["covered_lines"]' \
	'    total_lines = acc[package]["num_statements"]' \
	'    lines_percent = 100.0 if total_lines == 0 else (covered_lines / total_lines * 100)' \
	'    covered_branches = acc[package]["covered_branches"]' \
	'    total_branches = acc[package]["num_branches"]' \
	'    branches_percent = 100.0 if total_branches == 0 else (covered_branches / total_branches * 100)' \
	'    print(f"{package}\tlines {lines_percent:.2f}% ({covered_lines}/{total_lines})\tbranches {branches_percent:.2f}% ({covered_branches}/{total_branches})")' \
	| $(UV) python -

fix:
	@$(UV) ruff check . --fix
	@$(UV) ruff format .

repair: fix verify

verify-static: lint format-check typecheck

verify: verify-static test coverage-gate

clean:
	rm -f .coverage $(COVERAGE_JSON)
	rm -rf htmlcov
