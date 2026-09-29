# tests/chaos

Failure-injection tests, starting with the kill switch: every live process must halt within one second.

The tests live in `engine/crates/replay/tests/chaos.rs` because the engine workspace cannot contain packages outside `engine/`. They cover: kill switch engaged from another thread during a replay (halt observed in well under 1 s, no submission after), venue disconnect and reconnect, stale book, partial fills, and an unknown order at the venue.
