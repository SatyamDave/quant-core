// Verifies the v1.1 approval signature with node:crypto's Ed25519 support. The `hello` op
// hands the gateway a raw 32-byte Ed25519 public key (base64); node's classic crypto API only
// imports Ed25519 keys wrapped in a SubjectPublicKeyInfo (SPKI) DER envelope, not raw bytes, so
// we prepend the fixed 12-byte SPKI header for the Ed25519 OID (RFC 8410) -- a standard,
// well-known trick, not a home-grown crypto primitive.
import { createPublicKey, verify as cryptoVerify, type KeyObject } from "node:crypto";

const ED25519_RAW_PUBLIC_KEY_LEN = 32;
// DER: SEQUENCE { SEQUENCE { OID 1.3.101.112 }, BIT STRING (0 unused bits) <32-byte key> }
const SPKI_ED25519_PREFIX = Buffer.from("302a300506032b6570032100", "hex");

export function ed25519PublicKeyFromRaw(raw: Buffer): KeyObject {
  if (raw.length !== ED25519_RAW_PUBLIC_KEY_LEN) {
    throw new Error(`Ed25519 public key must be ${ED25519_RAW_PUBLIC_KEY_LEN} bytes, got ${raw.length}`);
  }
  const der = Buffer.concat([SPKI_ED25519_PREFIX, raw]);
  return createPublicKey({ key: der, format: "der", type: "spki" });
}

/** Never throws: a malformed signature/message (wrong length, etc.) is "does not verify", not
 *  a crash -- fail closed, and don't let a parse error look like "the check didn't run". */
export function verifyEd25519(publicKey: KeyObject, message: Buffer, signature: Buffer): boolean {
  try {
    return cryptoVerify(null, message, publicKey, signature);
  } catch {
    return false;
  }
}
