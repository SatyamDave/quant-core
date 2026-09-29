# data/recorders

Recorders write Parquet in the `data/schemas` format with both exchange and local timestamps. No recorder in this repository opens a live venue connection yet.

`synthetic.py` writes a deterministic synthetic day (seeded, no network) with the same schemas, for tests, the parity fixture, and `just walkforward`:

    cd research && PYTHONPATH=..:. uv run python -m data.recorders.synthetic --out ../data/raw/synthetic --seed 7

Output goes under `data/raw/`, which is gitignored. It is not a model of any venue.
