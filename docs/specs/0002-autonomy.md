> **Origin:** condensed by the coordinating agent from a prompt the repo owner pasted in chat on 2026-09-27; not the verbatim prompt. The full text is not in the repository.
>
> **Status:** Phase A only was built (#3, merged 2026-09-28). Phases B-H are deferred. The eval-suite size (30-100 tasks) is superseded by research-loop §7.

# quant-core autonomy spec (from the repo owner; runs after bootstrap)

Governing principle: **Autonomy is earned per category, measured continuously, and revoked automatically.** The system may do more on its own only where its measured track record justifies it, and never in the zones that can lose money or weaken safety.

Operating rules: plan in docs/plans/0002-autonomy.md first; one phase per branch/PR then stop for approval; never push to main or merge; verify every Claude Code, GitHub Actions and library feature against current docs. If native Claude Code scheduling/background agents exist and fit, prefer them over custom cron and record the choice in an ADR.

## 1. Four learners
| Learner | Improves | Memory | Signal |
|---|---|---|---|
| Knowledge | CLAUDE.md, rules, skills, runbooks | .claude/, nested CLAUDE.md, docs/ | human corrections, rejected PRs, CI failures, incidents |
| Research | which ideas are worth testing | experiment registry, graveyard, journal | validation results, shadow/live decay |
| Models | accuracy and edge | model registry | walk-forward metrics, shadow PnL, drift |
| Agents | prompts, skills, routing, budgets | agents/, scorecards, eval suite | PR acceptance, revert rate, eval scores, cost per merged PR |
Agent changes must beat the eval score to be kept.

## 2. Phase A — tiers and autonomy/POLICY.yaml
POLICY.yaml is protected (humans only; CODEOWNERS two approvals). Every automated action classified against it.
- T0 auto: records/knowledge that can't affect runtime; auto-merge when checks pass (registry entries, graveyard, journal, Learned sections, glossary, archive moves).
- T1 agent-reviewed auto: auto-merge after code-reviewer agent approves AND checks pass (new tests passing on main, flaky-test quarantine with issue, Dependabot patch bumps with green full suite, doc fixes, skill wording edits that don't regress evals).
- T2 human: one human approval (research code, ML pipeline code, new skills/agents, pre-live strategy code, non-protected engine code).
- T3 two humans + risk-auditor agent (protected zones, model promotion past shadow, strategy promotion to canary/scaled, anything in autonomy/).
Rules: default deny (unmatched = T2); tier promotion is a T3 human decision backed by Phase G metrics; automatic demotion one tier when a category's revert rate over last 30 merges exceeds policy threshold, opening an issue; nothing ever auto-merges into protected zones, .github/workflows/, .claude/settings.json, .claude/hooks/, autonomy/.
Enforced by autonomy-gate.yml: required check reading labels + diff, classifying, failing unless right approvals exist; the only thing allowed to enable auto-merge.
Phase A deliverable: plan + POLICY.yaml with conservative defaults (T0 only enabled at first), then stop.

## 3. Phase B — heartbeat loops (GitHub Actions schedule + anthropics/claude-code-action, or native scheduling; verify inputs)
loop-knowledge (on merge + daily; triage inbox, update CLAUDE.md/rules/skills; T0/T2 PRs) · loop-health (hourly; failing/flaky CI on main, broken links, stale TODOs, dead skills; T1/T2 PRs or issues) · loop-deps (daily; group Dependabot, full suite + scans, tier labels) · loop-research (nightly; top hypothesis, graveyard check, walk-forward, record; registry T0 + candidate PR or graveyard) · loop-discovery (nightly; RD-Agent/qlib sandbox, survivors to backlog) · loop-models (nightly/weekly; retrain challengers, validate vs champion, shadow comparisons; registry + T3 dossiers) · loop-backlog (daily; rank work items; issues labeled auto) · loop-meta (weekly; score agents, propose prompt/skill changes, run evals; T2 PRs with eval evidence) · loop-report (weekly; docs/reports/weekly/YYYY-WW.md T0).
Requirements: GitHub App installation token with minimal scopes (GITHUB_TOKEN PRs don't trigger workflows), documented in docs/runbooks/autonomy-bot.md; concurrency group per loop; max-turns cap, job timeout, daily dollar budget from POLICY.yaml, open-PR cap, clean exit + log on exhaustion; research/discovery/models on runners with no venue credentials or venue network access; global off switch: autonomy/PAUSE on main or repo variable AUTONOMY_ENABLED=false makes every loop exit in its first step; `just autonomy-pause`.

## 4. Phase C — backlog brain (agents/backlog/)
Signals: CI (repeat failures, slowest tests, bench regressions); code (TODO/FIXME, low coverage in critical crates, rising clippy/ruff warnings); postmortem open action items; research (journal next questions, near-miss experiments, discovery survivors); models (drift, champion decay, shadow-vs-backtest divergence); humans (issues labeled idea, review comments tagged later); scorecards (frequently failing skills).
Rank = expected value × confidence ÷ cost; formula + weights in POLICY.yaml. Dedupe vs open issues, graveyard, closed won't-fix. Each issue gets a task spec from agents/tasks/ and a tier label. Loops pick highest-ranked issue in their lane and link PRs; after two failed attempts mark needs-human with reason.

## 5. Phase D — outcomes (autonomy/outcomes/, append-only JSONL via `just outcome ...`)
Fields: loop, agent, task id, PR, tier, tokens + cost, wall time, result (merged/rejected/reverted/abandoned), reviewer comments summarized, CI failures, research gate failed + by how much.
Feedback: rejected/reverted → docs-gardener writes learnings proposal targeting the file that would have prevented it; same CI failure class 3× → T2 PR adding rule/pre-commit check/test; research gate failures → graveyard structured tags (feature family, horizon, venue, regime, failure gate), ranker down-weights similar; shadow/live underperforming backtest past threshold → finding about the backtest, fill/latency model work prioritized; postmortem action items must land as test/rule/runbook/monitor, checked monthly.

## 6. Phase E — agent evals (agents/evals/)
30–100 frozen tasks from real repo history (fix bug from commit X, add venue adapter method, reject overfit backtest, refuse to edit risk limits, catch seeded secret, detect lookahead). Automatic grader per task. Safety tasks must score 100%; any safety regression blocks. Track success, cost, turns, time. Run with headless Claude Code (verify flags) in isolated worktrees via `just evals` and loop-meta.
loop-meta protocol: find weakest skill/rule/prompt from scorecards + outcomes; propose change; eval baseline and candidate; open T2 PR only if target metric improves, aggregate success not regressed, safety 100%; PR body has eval table; record either way, failures in agents/evals/graveyard.md.
Every reverted PR and human correction becomes a candidate eval task (T2). Scorecards autonomy/scorecards/<agent>.md weekly (T0): acceptance, revert rate, cost per merged PR, eval trend, top failure reasons.

## 7. Phase F — memory architecture (docs/onboarding/how-this-repo-learns.md)
Root CLAUDE.md (permanent, humans prune) · nested Learned (max 15, gardener promotes/drops) · .claude/rules (permanent until superseded, gardener flags dead paths) · skills (versioned by evals, meta retires) · experiment registry, outcome log (append-only, never pruned) · graveyard (never pruned; may mark revisit with reason) · journal (permanent) · scorecards + weekly reports (regenerated).
Hygiene: CLAUDE.md line budgets stay enforced; a lesson in 3+ Learned sections promoted to root rule or skill via T2; after each major model release loop-meta runs evals and proposes deleting instructions no longer needed.

## 8. Phase G — metrics (autonomy/metrics/ → docs/reports/weekly/ + dashboard in ops/monitoring/)
Engineering: acceptance + revert rate per tier/loop, median signal-to-merged-fix time, CI red time on main, known vuln count (zero High/Critical). Knowledge: repeat-mistake rate after lesson recorded, share of CLAUDE.md content referencing live paths. Research: hypotheses/week, gate pass rate, deflated Sharpe of survivors with true trial count, duplicate-of-graveyard rate. Models: champion age, challenger win rate, shadow-vs-backtest gap. Agents: eval trend, cost per merged PR, safety score (100%). Cost: spend per loop per week vs budget. Tier promotions only proposed with these numbers.

## 9. Phase H — circuit breakers (build before enabling anything; each has a trip test in tests/autonomy/)
revert-rate (loop pauses + needs-human issue) · cost (daily/monthly per loop + global caps pause loops) · churn (same file modified by automation > N times in 7 days blocks further edits + escalates) · safety-eval (pauses loop-meta, blocks agent-config changes until human clears) · scope (automated PR outside tier allowlist auto-closed, logged as incident) · model (drift/decay demotes to no signal; demotion always allowed, promotion never) · injection (external-content loop output containing agent-directed instructions or permission requests is quarantined + alert).

## 10. Acceptance
POLICY.yaml protected + autonomy-gate.yml required; T0 registry PR auto-merges, T2 code PR doesn't, config/limits/ PR blocked even with one approval; bot PRs trigger required checks; autonomy/PAUSE stops every loop first step; loop-backlog ranked deduped issues from ≥3 sources; loop-research one hypothesis end-to-end; rejected PR yields learnings proposal targeting right file; `just evals` with safety 100% and eval table in a loop-meta PR; every breaker trip test passes; first weekly report with real metrics + costs; how-this-repo-learns.md understandable in 15 minutes.

## 11. Do NOT
No loop holds venue credentials, deploys, or changes risk limits; no loop edits autonomy/, workflows, hooks, or settings on its own. No auto-promotion of model/strategy past shadow under any metric. No agent change counted as improvement without eval evidence. No unbounded growth of always-loaded context. Never disable a breaker to make a loop run; stop and ask.
