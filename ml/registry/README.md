# ml/registry

Model registry records (`ModelRecord`): experiment, artifact sha256, status (`challenger`, `no_signal`, `promoted`), dataset hash, feature version, full training config, metrics, lineage, and any approval. Appended to `research/registry/log/models.jsonl` next to the trial log `trials.jsonl`; set `QC_REGISTRY_DIR` to write elsewhere.

Decision: this uses the existing append-only JSONL registry, not MLflow, to avoid a service and a large dependency before there is more than one training machine. A status change is a new record for the same artifact hash; `latest` returns the current one. Move to an MLflow tracking server when several machines train at once or a UI is needed.
