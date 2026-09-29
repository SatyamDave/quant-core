//! Multinomial linear model loaded from an `ml/export` JSON artifact.

use std::fmt::{self, Write as _};

use serde::Deserialize;
use sha2::{Digest, Sha256};

use crate::features::{FEATURE_NAMES, FEATURE_VERSION, N_FEATURES};
use crate::{Direction, Model, Signal};

const FORMAT: &str = "qc-linear-v1";
const CLASSES: [&str; 3] = ["down", "flat", "up"];

#[derive(Debug, PartialEq, Eq)]
pub enum LoadError {
    /// The artifact bytes do not hash to the registry's expected value.
    HashMismatch {
        expected: String,
        actual: String,
    },
    Parse(String),
    /// Parsed, but not a model this engine build can run.
    Incompatible(String),
}

impl fmt::Display for LoadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::HashMismatch { expected, actual } => {
                write!(
                    f,
                    "artifact sha256 {actual} does not match registry {expected}"
                )
            }
            Self::Parse(e) => write!(f, "artifact is not valid JSON: {e}"),
            Self::Incompatible(e) => write!(f, "artifact is incompatible: {e}"),
        }
    }
}

impl std::error::Error for LoadError {}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Artifact {
    format: String,
    feature_version: String,
    features: Vec<String>,
    classes: Vec<String>,
    coef: Vec<Vec<f64>>,
    intercept: Vec<f64>,
}

#[derive(Debug, Clone)]
pub struct LinearModel {
    coef: [[f64; N_FEATURES]; 3],
    intercept: [f64; 3],
}

#[must_use]
pub fn sha256_hex(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .fold(String::with_capacity(64), |mut s, b| {
            let _ = write!(s, "{b:02x}");
            s
        })
}

impl LinearModel {
    /// Loads an artifact only if its sha256 equals `expected_sha256` from the
    /// model registry and it matches this build's feature set.
    ///
    /// # Errors
    /// Any hash mismatch, parse failure, or incompatibility; the caller then
    /// runs with no model, which means no signal.
    pub fn load(bytes: &[u8], expected_sha256: &str) -> Result<Self, LoadError> {
        let actual = sha256_hex(bytes);
        if actual != expected_sha256.trim() {
            return Err(LoadError::HashMismatch {
                expected: expected_sha256.trim().to_owned(),
                actual,
            });
        }
        let a: Artifact =
            serde_json::from_slice(bytes).map_err(|e| LoadError::Parse(e.to_string()))?;
        let bad = |what: &str| Err(LoadError::Incompatible(what.to_owned()));
        if a.format != FORMAT {
            return bad("format");
        }
        if a.feature_version != FEATURE_VERSION || a.features != FEATURE_NAMES {
            return bad("feature set");
        }
        if a.classes != CLASSES {
            return bad("classes");
        }
        let finite = |v: &[f64]| v.iter().all(|x| x.is_finite());
        let (Ok(intercept), true) = (
            <[f64; 3]>::try_from(a.intercept.as_slice()),
            a.coef.len() == 3,
        ) else {
            return bad("shape");
        };
        let mut coef = [[0.0; N_FEATURES]; 3];
        for (dst, row) in coef.iter_mut().zip(&a.coef) {
            let Ok(r) = <[f64; N_FEATURES]>::try_from(row.as_slice()) else {
                return bad("shape");
            };
            *dst = r;
        }
        if !finite(&intercept) || !coef.iter().all(|r| finite(r)) {
            return bad("non-finite weight");
        }
        Ok(Self { coef, intercept })
    }
}

impl Model for LinearModel {
    fn predict(&self, features: &[f64]) -> Option<Signal> {
        let x = <&[f64; N_FEATURES]>::try_from(features).ok()?;
        if !x.iter().all(|v| v.is_finite()) {
            return None;
        }
        let mut z = self.intercept;
        for (zk, w) in z.iter_mut().zip(&self.coef) {
            *zk += w.iter().zip(x).map(|(w, x)| w * x).sum::<f64>();
        }
        let max = z.iter().copied().fold(f64::NEG_INFINITY, f64::max);
        let e = z.map(|v| (v - max).exp());
        let total: f64 = e.iter().sum();
        let probs = e.map(|v| v / total);
        if !probs.iter().all(|p| p.is_finite()) {
            return None;
        }
        let best = (0..3).fold(0, |b, i| if probs[i] > probs[b] { i } else { b });
        let direction = [Direction::Down, Direction::Flat, Direction::Up][best];
        Some(Signal { direction, probs })
    }
}
