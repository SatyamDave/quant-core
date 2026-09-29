---
name: replay-debug
description: Debug a replay determinism failure. Use when `just replay` fails, two runs produce different order logs, or a replay test is red.
---
## Steps
1. Run `just replay` twice and diff the order logs to find the first divergent order. (`just replay` is a placeholder until the engine-core phase implements it.)
2. Check the usual causes: `HashMap`/`HashSet` iteration, wall-clock time instead of `SimClock`, floats, thread scheduling, unseeded randomness.
3. Write a failing test that reproduces the divergence before fixing.
4. A replay failure blocks merge (rule 7).
