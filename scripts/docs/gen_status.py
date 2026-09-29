"""Regenerate docs/STATUS.md and the live-status block in the project guide.

Reads only the checked-out tree, `git log`, and two JSON files the workflow
fetches with `gh` (PR list, latest CI run on main). It prints no wall-clock
time: the output depends only on its inputs, so an unchanged repo produces an
unchanged file and the workflow commits nothing.

PR titles are untrusted text (anyone can open a PR); they are escaped for both
Markdown tables and HTML before they are written.

Usage: python3 scripts/docs/gen_status.py [--prs prs.json] [--ci ci.json]
"""

import argparse
import html
import json
import re
import subprocess
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATUS_MD = ROOT / "docs" / "STATUS.md"
GUIDE = ROOT / "docs" / "guide" / "quant-core-guide.html"
START, END = "<!-- AUTO-STATUS:START -->", "<!-- AUTO-STATUS:END -->"
REPO_URL = "https://github.com/SatyamDave/quant-core"


def md_cell(text: str) -> str:
    return (
        re.sub(r"[\r\n]+", " ", text).replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")
    )


def git(*args: str) -> str:
    cmd = ["git", "-C", str(ROOT), *args]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True)  # noqa: S603 - fixed git command
    return out.stdout.strip()


def load_json(path: str | None) -> list[dict]:
    if not path or not Path(path).exists():
        return []
    data = json.loads(Path(path).read_text())
    return data if isinstance(data, list) else []


def adrs() -> list[tuple[str, str, str]]:
    rows = []
    for f in sorted((ROOT / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md")):
        if f.name.startswith("0000"):
            continue
        text = f.read_text()
        title = text.splitlines()[0].lstrip("# ").strip()
        m = re.search(r"^## Status\s*\n+(.+)$", text, re.M) or re.search(
            r"^\*?\*?Status\*?\*?:?\s*(.+)$", text, re.M
        )
        status = m.group(1).strip() if m else "unknown"
        rows.append((f.name, title, status.split(".")[0]))
    return rows


def test_counts() -> dict[str, int]:
    rust = sum(
        len(re.findall(r"#\[test\]", p.read_text()))
        for p in (ROOT / "engine").rglob("*.rs")
        if "target" not in p.parts
    )
    py = sum(
        len(re.findall(r"^\s*def test_", p.read_text(), re.M))
        for p in ROOT.rglob("test_*.py")
        if not {".venv", "node_modules", "out"} & set(p.parts)
    )
    return {"Rust #[test] functions": rust, "Python test functions": py}


def ledger() -> Counter:
    counts: Counter = Counter()
    trials = ROOT / "research" / "registry" / "log" / "trials.jsonl"
    if trials.exists():
        for line in trials.read_text().splitlines():
            if line.strip():
                counts[json.loads(line).get("experiment", "?")] += 1
    return counts


def policy_enabled() -> str:
    p = ROOT / "autonomy" / "POLICY.yaml"
    if not p.exists():
        return "no policy file"
    m = re.search(r'^\s*"enabled":\s*(true|false)', p.read_text(), re.M)
    return "on" if m and m.group(1) == "true" else "off"


def pr_link(p: dict) -> str:
    return f"[#{p['number']}]({REPO_URL}/pull/{p['number']})"


def status_markdown(ctx: dict) -> str:
    md = [
        "# Live status",
        "",
        "Regenerated automatically by `.github/workflows/docs-status.yml` on every merge to",
        "`main`, and every 30 minutes for PR changes. Do not edit by hand; edit",
        "`scripts/docs/gen_status.py`.",
        "Narrative docs: [README](../README.md).",
        "",
        f"Generated from `main` at **{ctx['head']}**. Latest CI on `main`: **{ctx['ci']}**.",
        f"Paid AI loops: **{ctx['policy']}**.",
        "",
        f"## Open pull requests ({len(ctx['open'])})",
        "",
    ]
    if ctx["open"]:
        md += ["| PR | Title | Author | Draft |", "|---|---|---|---|"]
        for p in ctx["open"]:
            author = md_cell((p.get("author") or {}).get("login", "?"))
            draft = "yes" if p.get("isDraft") else "no"
            md.append(f"| {pr_link(p)} | {md_cell(p.get('title', ''))} | {author} | {draft} |")
    else:
        md.append("None.")
    md += ["", "## Recently merged", "", "| PR | Title | Merged |", "|---|---|---|"]
    for p in ctx["merged"]:
        md.append(
            f"| {pr_link(p)} | {md_cell(p.get('title', ''))} | {(p.get('mergedAt') or '')[:10]} |"
        )
    md += ["", "## Recent commits on main", "", "| Commit | Date | Subject |", "|---|---|---|"]
    md += [f"| `{c[0]}` | {c[1]} | {md_cell(c[2])} |" for c in ctx["commits"] if len(c) == 3]
    md += ["", "## Decisions (ADRs)", "", "| ADR | Title | Status |", "|---|---|---|"]
    md += [f"| [{n}](adr/{n}) | {md_cell(t)} | {md_cell(st)} |" for n, t, st in ctx["adrs"]]
    trials = ", ".join(f"`{x}` {n}" for x, n in sorted(ctx["trials"].items())) or "none"
    md += [
        "",
        "## Code and research",
        "",
        "- Engine crates: " + ", ".join(f"`{c}`" for c in ctx["crates"]),
    ]
    md += [f"- {k}: {v}" for k, v in ctx["tests"].items()]
    md += [f"- Registry trials: {trials}", ""]
    return "\n".join(md)


def status_html(ctx: dict) -> str:
    e = html.escape

    def items(values: list[str]) -> str:
        return "".join(f"<li>{v}</li>" for v in values) or "<li>none</li>"

    open_items = [f"#{p['number']} {e(p.get('title', ''))}" for p in ctx["open"]]
    merged_items = [
        f"#{p['number']} {e(p.get('title', ''))} ({e((p.get('mergedAt') or '')[:10])})"
        for p in ctx["merged"][:8]
    ]
    adr_items = [f"{e(t)}: {e(st)}" for _, t, st in ctx["adrs"]]
    tests = e(", ".join(f"{k}: {v}" for k, v in ctx["tests"].items()))
    trials = e(", ".join(f"{x} {n}" for x, n in sorted(ctx["trials"].items())) or "none")
    return "\n".join(
        [
            START,
            '<div class="card"><p class="small">Updated automatically on every merge to main and '
            "every 30 minutes by <code>.github/workflows/docs-status.yml</code>. Full table: "
            "<code>docs/STATUS.md</code>.</p>",
            f"<p><b>main</b> at <code>{e(ctx['head'])}</code> · latest CI: <b>{e(ctx['ci'])}</b>"
            f" · paid AI loops: <b>{e(ctx['policy'])}</b></p>",
            f"<p><b>Open PRs ({len(ctx['open'])})</b></p><ul>{items(open_items)}</ul>",
            f"<p><b>Recently merged</b></p><ul>{items(merged_items)}</ul>",
            f"<p><b>Decisions</b></p><ul>{items(adr_items)}</ul>",
            f'<p class="small">{tests} · trials: {trials}</p></div>',
            END,
        ]
    )


def build(prs: list[dict], ci: list[dict]) -> tuple[str, str]:
    log = git("log", "-12", "--format=%h\t%ad\t%s", "--date=short")
    ci_state = "not fetched"
    if ci:
        result = ci[0].get("conclusion") or ci[0].get("status", "?")
        ci_state = f"{result} on {ci[0].get('headSha', '')[:7]}"
    merged = [p for p in prs if p.get("state") == "MERGED"]
    ctx = {
        "head": git("log", "-1", "--format=%h %ad", "--date=short"),
        "commits": [ln.split("\t", 2) for ln in log.splitlines()],
        "open": sorted((p for p in prs if p.get("state") == "OPEN"), key=lambda p: -p["number"]),
        "merged": sorted(merged, key=lambda p: p.get("mergedAt") or "", reverse=True)[:15],
        "ci": ci_state,
        "policy": policy_enabled(),
        "tests": test_counts(),
        "trials": ledger(),
        "adrs": adrs(),
        "crates": sorted(
            p.name for p in (ROOT / "engine" / "crates").iterdir() if (p / "Cargo.toml").exists()
        ),
    }
    return status_markdown(ctx), status_html(ctx)


def inject(page: str, block: str) -> str:
    if START not in page or END not in page:
        raise SystemExit(f"{GUIDE}: missing {START} / {END} markers")
    return page[: page.index(START)] + block + page[page.index(END) + len(END) :]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prs")
    ap.add_argument("--ci")
    args = ap.parse_args()
    md, block = build(load_json(args.prs), load_json(args.ci))
    STATUS_MD.write_text(md)
    if GUIDE.is_file():  # optional HTML guide; the template ships without one
        GUIDE.write_text(inject(GUIDE.read_text(), block))


if __name__ == "__main__":
    main()
