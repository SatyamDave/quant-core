import { generateKeyPairSync, sign as cryptoSign } from "node:crypto";
import { describe, expect, it } from "vitest";
import { ed25519PublicKeyFromRaw, verifyEd25519 } from "../../src/broker/crypto.js";

function rawPublicKeyBytes(spkiDer: Buffer): Buffer {
  // The fixed 12-byte SPKI prefix for Ed25519, then the 32-byte raw key -- see crypto.ts.
  return spkiDer.subarray(spkiDer.length - 32);
}

describe("ed25519 raw-key verification", () => {
  const { publicKey, privateKey } = generateKeyPairSync("ed25519");
  const rawPub = rawPublicKeyBytes(publicKey.export({ type: "spki", format: "der" }) as Buffer);
  const message = Buffer.from('{"a":1}', "utf8");
  const signature = cryptoSign(null, message, privateKey);

  it("verifies a real signature against the raw public key", () => {
    const key = ed25519PublicKeyFromRaw(rawPub);
    expect(verifyEd25519(key, message, signature)).toBe(true);
  });

  it("rejects a signature over different bytes", () => {
    const key = ed25519PublicKeyFromRaw(rawPub);
    expect(verifyEd25519(key, Buffer.from('{"a":2}', "utf8"), signature)).toBe(false);
  });

  it("rejects a corrupted signature without throwing", () => {
    const key = ed25519PublicKeyFromRaw(rawPub);
    const corrupted = Buffer.from(signature);
    corrupted[0] = (corrupted[0]! + 1) % 256;
    expect(verifyEd25519(key, message, corrupted)).toBe(false);
  });

  it("rejects a malformed signature buffer without throwing", () => {
    const key = ed25519PublicKeyFromRaw(rawPub);
    expect(verifyEd25519(key, message, Buffer.from("too short"))).toBe(false);
  });

  it("throws when the raw key is not 32 bytes", () => {
    expect(() => ed25519PublicKeyFromRaw(Buffer.alloc(31))).toThrow(/32 bytes/);
  });
});
