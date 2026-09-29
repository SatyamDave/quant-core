---
name: promotion-dossier
description: Prepare a promotion dossier for a model. Use when a challenger has passed validation and shadow mode and someone asks to promote it.
---
## Steps
1. Collect from the registry: dataset hash, feature version, metrics, artifact hash, lineage, trial count.
2. Show validation results against every gate (deflated Sharpe, PBO, regime stability, cost sensitivity) versus the champion on identical data.
3. Show shadow-mode results: live predictions vs realized edge, drift metrics.
4. Confirm the exported artifact hash matches what engine/crates/inference will load.
5. Write the dossier and request human approval. The agent never promotes; next step after approval is canary.
