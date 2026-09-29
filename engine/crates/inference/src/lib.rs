//! Model inference inside the engine. Models come only from `ml/export` with a
//! matching registry hash. When a model cannot answer, the answer is no signal
//! (`None`), never a guess.

pub mod features;
pub mod linear;

/// Ternary direction over the model's label horizon, net of the half-spread.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Direction {
    Down,
    Flat,
    Up,
}

/// A model's view of the next move. `probs` are P(down), P(flat), P(up).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Signal {
    pub direction: Direction,
    pub probs: [f64; 3],
}

/// A loaded model. `predict` must be deterministic and within its latency
/// budget; on missing features, a stale model, or any doubt it returns `None`.
pub trait Model {
    fn predict(&self, features: &[f64]) -> Option<Signal>;
}
