---
name: latency-profile
description: Profile and benchmark hot-path latency. Use when a change may affect latency, a bench regressed, or asked to measure performance in engine/.
---
## Steps
1. Run `just bench` on the base branch and save the criterion baseline.
2. Apply the change and run `just bench` again; criterion reports the difference.
3. A regression over 5% fails the PR; report before and after numbers in the PR.
4. Look for allocation, locks, syscalls, or formatting on the per-event path first.
5. Never claim a speedup without the measured numbers.
