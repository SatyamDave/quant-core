//! `qc-bridge`: a JSON Lines stdio bridge between the deterministic Rust
//! engine and an external decision agent (protocol v1, `schemas/decision/v1`).
//! Stdout carries only protocol response lines; everything else (parse
//! errors, model load failures) goes to stderr, so a caller reading stdout
//! never has to distinguish a log line from a response.

pub mod approval;
pub mod engine;
pub mod eventlog;
pub mod feed;
pub mod instrument;
pub mod wire;

pub use engine::{BridgeEngine, HaltReason, LoadedModel, VenueMode};
pub use wire::handle_line;
