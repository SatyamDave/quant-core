//! Ed25519 approval tokens (protocol v1.1, issue #34): a short-lived,
//! tamper-proof stamp bound to exactly one approved order intent, so the
//! piece that talks to a venue (the TS gateway, `agent/src/broker/`) can act
//! on risk's decision without re-implementing or trusting it itself. `mint`
//! and `verify` share one canonicalization ([`ApprovalPayload`]'s field
//! order, which is already alphabetical), so a Rust-signed approval and a
//! TypeScript-verified one never disagree about what bytes were signed.
//!
//! The bridge only ever calls `mint` (it never receives its own approvals
//! back); `verify` exists here so it has exactly one implementation, tested
//! against real Rust-signed output, and shipped as `approval-test-vector.json`
//! for the TS gateway to test against.

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD as BASE64;
use ed25519_dalek::{Signature, Signer as _, SigningKey, Verifier as _, VerifyingKey};
use qc_core::{Price, Qty};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

/// 5 seconds of sim/market time, per `schemas/decision/v1/README.md`.
pub const APPROVAL_TTL_NS: u64 = 5_000_000_000;

/// Generates a fresh signing key from the OS CSPRNG. Called once per bridge
/// process (root rule: no wall clock, but randomness for a signing key is
/// not a time source and does not affect replay determinism in `sim` mode,
/// where no approval is ever minted).
///
/// # Panics
/// If the OS random source fails (`getrandom::fill`), which would mean the
/// process cannot safely mint approvals at all.
#[must_use]
pub fn generate_signing_key() -> SigningKey {
    let mut seed = [0u8; 32];
    getrandom::fill(&mut seed).expect("OS random source is required to mint approval tokens");
    SigningKey::from_bytes(&seed)
}

#[must_use]
pub fn encode_public_key(key: &VerifyingKey) -> String {
    BASE64.encode(key.to_bytes())
}

/// The order fields an approval is bound to. Built from exactly what was
/// submitted (never re-derived), so a stamp minted for one order can never
/// be replayed against a changed one.
#[derive(Debug, Clone, Copy)]
pub struct OrderFields<'a> {
    pub client_order_id: u64,
    pub request_id: &'a str,
    pub instrument: &'a str,
    /// `"buy"` | `"sell"`, the wire spelling.
    pub side: &'static str,
    pub qty: Qty,
    pub limit_price: Price,
    /// `"ioc"` | `"gtc"`, the wire spelling.
    pub time_in_force: &'static str,
}

/// The canonical payload object. Field order here **is** the canonical wire
/// order (already alphabetical: c < e < i < l < q < r < s < t), so
/// `serde_json::to_string` — compact by default, no whitespace — produces
/// exactly the bytes `schemas/decision/v1/README.md` specifies without a
/// second sorting step.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct ApprovalPayload {
    client_order_id: u64,
    expires_ts_ns: u64,
    instrument: String,
    limit_price: String,
    qty: String,
    request_id: String,
    side: String,
    time_in_force: String,
}

impl ApprovalPayload {
    fn from_fields(order: &OrderFields<'_>, expires_ts_ns: u64) -> Self {
        Self {
            client_order_id: order.client_order_id,
            expires_ts_ns,
            instrument: order.instrument.to_owned(),
            limit_price: order.limit_price.to_string(),
            qty: order.qty.to_string(),
            request_id: order.request_id.to_owned(),
            side: order.side.to_owned(),
            time_in_force: order.time_in_force.to_owned(),
        }
    }

    /// Every field must match exactly, or the stamp does not belong to this order.
    fn matches(&self, order: &OrderFields<'_>) -> Result<(), VerifyError> {
        if self.client_order_id != order.client_order_id {
            return Err(VerifyError::FieldMismatch("client_order_id"));
        }
        if self.instrument != order.instrument {
            return Err(VerifyError::FieldMismatch("instrument"));
        }
        if self.limit_price != order.limit_price.to_string() {
            return Err(VerifyError::FieldMismatch("limit_price"));
        }
        if self.qty != order.qty.to_string() {
            return Err(VerifyError::FieldMismatch("qty"));
        }
        if self.request_id != order.request_id {
            return Err(VerifyError::FieldMismatch("request_id"));
        }
        if self.side != order.side {
            return Err(VerifyError::FieldMismatch("side"));
        }
        if self.time_in_force != order.time_in_force {
            return Err(VerifyError::FieldMismatch("time_in_force"));
        }
        Ok(())
    }
}

/// Signs `order`, expiring [`APPROVAL_TTL_NS`] after `decision_ts_ns` (sim or
/// market time, never a wall-clock read). Returns the wire-shaped
/// `{"payload", "signature"}` object (`schemas/decision/v1/approval.schema.json`).
///
/// # Panics
/// Never in practice: [`ApprovalPayload`] is a plain struct of strings and
/// integers, which always serializes.
#[must_use]
pub fn mint(key: &SigningKey, order: &OrderFields<'_>, decision_ts_ns: u64) -> Value {
    let payload = ApprovalPayload::from_fields(order, decision_ts_ns + APPROVAL_TTL_NS);
    let payload_json = serde_json::to_string(&payload).expect("ApprovalPayload always serializes");
    let signature = key.sign(payload_json.as_bytes());
    json!({
        "payload": payload_json,
        "signature": BASE64.encode(signature.to_bytes()),
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VerifyError {
    /// `"payload"`/`"signature"` missing or not strings.
    Malformed,
    /// The signature does not verify against the given public key and
    /// payload bytes — covers both a bad key and any tamper of the payload
    /// string (even one byte breaks verification).
    BadSignature,
    /// The payload verified but is not the canonical JSON this module emits.
    BadPayloadJson,
    /// The payload verified and parsed, but does not describe `order` — a
    /// stamp minted for a different instrument, side, qty, price or id.
    FieldMismatch(&'static str),
    /// The payload verified and matches, but `now_ns >= expires_ts_ns`.
    Expired,
}

/// What the TS gateway (`agent/src/broker/`) must do before acting on an
/// approval, mirrored here so it has one tested implementation on the Rust
/// side and a shared test vector (`approval-test-vector.json`) to check the
/// TS port against: verify the signature against the hello public key,
/// check every field matches the order about to be sent, and check the
/// payload has not expired as of `now_ns`.
///
/// # Errors
/// See [`VerifyError`].
pub fn verify(
    public_key: &VerifyingKey,
    approval: &Value,
    order: &OrderFields<'_>,
    now_ns: u64,
) -> Result<(), VerifyError> {
    let payload_json = approval
        .get("payload")
        .and_then(Value::as_str)
        .ok_or(VerifyError::Malformed)?;
    let signature_b64 = approval
        .get("signature")
        .and_then(Value::as_str)
        .ok_or(VerifyError::Malformed)?;
    let signature_bytes: [u8; 64] = BASE64
        .decode(signature_b64)
        .map_err(|_| VerifyError::Malformed)?
        .try_into()
        .map_err(|_| VerifyError::Malformed)?;
    let signature = Signature::from_bytes(&signature_bytes);
    public_key
        .verify(payload_json.as_bytes(), &signature)
        .map_err(|_| VerifyError::BadSignature)?;

    let payload: ApprovalPayload =
        serde_json::from_str(payload_json).map_err(|_| VerifyError::BadPayloadJson)?;
    payload.matches(order)?;
    if now_ns >= payload.expires_ts_ns {
        return Err(VerifyError::Expired);
    }
    Ok(())
}

/// `cancel_order_intent`'s approval fields (protocol v1.2, extended
/// additively by v1.2.1/issue #44's cancel-on-halt gap): the id, plus an
/// optional provenance tag for *why* this cancel was minted. Nothing here is
/// checked by [`verify_cancel`] beyond `client_order_id` -- `reason` is
/// audit-only, so a broker/ops log can tell a mass halt-cancel apart from an
/// agent-requested one.
#[derive(Debug, Clone, Copy)]
pub struct CancelFields {
    pub client_order_id: u64,
    /// `Some("halt")` when the bridge halted and requested this order be
    /// canceled as part of cancelling every open order
    /// (`BridgeEngine::cancel_all_open`); `None` for an explicit
    /// `cancel_order_intent` call, which keeps this struct's and the wire
    /// payload's v1.2 shape byte-for-byte unchanged.
    pub reason: Option<&'static str>,
}

/// The canonical cancel-approval payload. Field order here **is** the
/// canonical wire order (already alphabetical: a < c < e < r), matching
/// `schemas/decision/v1/README.md`'s v1.2 section. `reason` is v1.2.1's
/// additive field: omitted from the wire entirely when `None` (`serde`'s
/// `skip_serializing_if`), so an explicit `cancel_order_intent`'s approval is
/// byte-for-byte identical to before this field existed, and `#[serde(default)]`
/// means a payload minted before this field existed still deserializes here.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct CancelApprovalPayload {
    action: String,
    client_order_id: u64,
    expires_ts_ns: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    reason: Option<String>,
}

/// Signs a cancel (protocol v1.2), same key and TTL as [`mint`].
///
/// # Panics
/// Never in practice, for the same reason as [`mint`].
#[must_use]
pub fn mint_cancel(key: &SigningKey, cancel: &CancelFields, decision_ts_ns: u64) -> Value {
    let payload = CancelApprovalPayload {
        action: "cancel".to_owned(),
        client_order_id: cancel.client_order_id,
        expires_ts_ns: decision_ts_ns + APPROVAL_TTL_NS,
        reason: cancel.reason.map(str::to_owned),
    };
    let payload_json =
        serde_json::to_string(&payload).expect("CancelApprovalPayload always serializes");
    let signature = key.sign(payload_json.as_bytes());
    json!({
        "payload": payload_json,
        "signature": BASE64.encode(signature.to_bytes()),
    })
}

/// The TS gateway's own cancel-approval check, mirrored here for the same
/// reason as [`verify`]: one tested implementation, one shared test vector.
///
/// # Errors
/// See [`VerifyError`].
pub fn verify_cancel(
    public_key: &VerifyingKey,
    approval: &Value,
    cancel: &CancelFields,
    now_ns: u64,
) -> Result<(), VerifyError> {
    let payload_json = approval
        .get("payload")
        .and_then(Value::as_str)
        .ok_or(VerifyError::Malformed)?;
    let signature_b64 = approval
        .get("signature")
        .and_then(Value::as_str)
        .ok_or(VerifyError::Malformed)?;
    let signature_bytes: [u8; 64] = BASE64
        .decode(signature_b64)
        .map_err(|_| VerifyError::Malformed)?
        .try_into()
        .map_err(|_| VerifyError::Malformed)?;
    let signature = Signature::from_bytes(&signature_bytes);
    public_key
        .verify(payload_json.as_bytes(), &signature)
        .map_err(|_| VerifyError::BadSignature)?;

    let payload: CancelApprovalPayload =
        serde_json::from_str(payload_json).map_err(|_| VerifyError::BadPayloadJson)?;
    if payload.action != "cancel" || payload.client_order_id != cancel.client_order_id {
        return Err(VerifyError::FieldMismatch("client_order_id"));
    }
    if now_ns >= payload.expires_ts_ns {
        return Err(VerifyError::Expired);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixed_key() -> SigningKey {
        // Test-only fixed seed (never used outside this module's tests and
        // `approval-test-vector.json`, which was generated from this exact
        // seed — see that file's own comment).
        SigningKey::from_bytes(&[7u8; 32])
    }

    fn sample_order() -> OrderFields<'static> {
        OrderFields {
            client_order_id: 1,
            request_id: "dr-1",
            instrument: "1",
            side: "buy",
            qty: "0.001".parse().unwrap(),
            limit_price: "99.80000000".parse().unwrap(),
            time_in_force: "gtc",
        }
    }

    #[test]
    fn mints_and_verifies_a_matching_order() {
        let key = fixed_key();
        let order = sample_order();
        let approval = mint(&key, &order, 1_000_000_000);
        assert!(verify(&key.verifying_key(), &approval, &order, 1_000_000_001).is_ok());
    }

    #[test]
    fn tampering_the_payload_fails_signature_verification() {
        let key = fixed_key();
        let order = sample_order();
        let mut approval = mint(&key, &order, 1_000_000_000);
        let payload = approval["payload"]
            .as_str()
            .unwrap()
            .replace("0.001", "9.999");
        approval["payload"] = json!(payload);
        assert_eq!(
            verify(&key.verifying_key(), &approval, &order, 1_000_000_001),
            Err(VerifyError::BadSignature)
        );
    }

    #[test]
    fn reusing_a_stamp_on_a_different_order_is_rejected() {
        let key = fixed_key();
        let order = sample_order();
        let approval = mint(&key, &order, 1_000_000_000);
        let mut changed = order;
        changed.qty = "1000".parse().unwrap();
        assert_eq!(
            verify(&key.verifying_key(), &approval, &changed, 1_000_000_001),
            Err(VerifyError::FieldMismatch("qty"))
        );
    }

    #[test]
    fn an_expired_stamp_is_rejected() {
        let key = fixed_key();
        let order = sample_order();
        let decision_ts_ns = 1_000_000_000;
        let approval = mint(&key, &order, decision_ts_ns);
        let expires_ts_ns = decision_ts_ns + APPROVAL_TTL_NS;
        assert_eq!(
            verify(&key.verifying_key(), &approval, &order, expires_ts_ns),
            Err(VerifyError::Expired)
        );
    }

    #[test]
    fn mints_and_verifies_a_matching_cancel() {
        let key = fixed_key();
        let cancel = CancelFields {
            client_order_id: 7,
            reason: None,
        };
        let approval = mint_cancel(&key, &cancel, 1_000_000_000);
        assert!(verify_cancel(&key.verifying_key(), &approval, &cancel, 1_000_000_001).is_ok());
    }

    #[test]
    fn a_cancel_stamp_does_not_verify_against_a_different_order_id() {
        let key = fixed_key();
        let cancel = CancelFields {
            client_order_id: 7,
            reason: None,
        };
        let approval = mint_cancel(&key, &cancel, 1_000_000_000);
        let other = CancelFields {
            client_order_id: 8,
            reason: None,
        };
        assert_eq!(
            verify_cancel(&key.verifying_key(), &approval, &other, 1_000_000_001),
            Err(VerifyError::FieldMismatch("client_order_id"))
        );
    }

    #[test]
    fn a_halt_reason_is_carried_in_the_wire_payload_and_still_verifies() {
        let key = fixed_key();
        let cancel = CancelFields {
            client_order_id: 7,
            reason: Some("halt"),
        };
        let approval = mint_cancel(&key, &cancel, 1_000_000_000);
        assert!(verify_cancel(&key.verifying_key(), &approval, &cancel, 1_000_000_001).is_ok());
        let payload = approval["payload"].as_str().unwrap();
        assert!(payload.contains("\"reason\":\"halt\""), "{payload}");
    }

    #[test]
    fn an_explicit_cancels_payload_omits_reason_entirely_unchanged_from_v1_2() {
        let key = fixed_key();
        let cancel = CancelFields {
            client_order_id: 7,
            reason: None,
        };
        let approval = mint_cancel(&key, &cancel, 1_000_000_000);
        let payload = approval["payload"].as_str().unwrap();
        assert_eq!(
            payload,
            r#"{"action":"cancel","client_order_id":7,"expires_ts_ns":6000000000}"#
        );
    }

    #[test]
    fn an_expired_cancel_stamp_is_rejected() {
        let key = fixed_key();
        let cancel = CancelFields {
            client_order_id: 7,
            reason: None,
        };
        let decision_ts_ns = 1_000_000_000;
        let approval = mint_cancel(&key, &cancel, decision_ts_ns);
        let expires_ts_ns = decision_ts_ns + APPROVAL_TTL_NS;
        assert_eq!(
            verify_cancel(&key.verifying_key(), &approval, &cancel, expires_ts_ns),
            Err(VerifyError::Expired)
        );
    }
}
