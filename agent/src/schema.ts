// Strict ajv validation against schemas/decision/v1/*.schema.json (JSON Schema 2020-12).
// One Ajv instance holds every schema so the cross-file $ref (e.g. bridge_response ->
// decision_request) resolves against each schema's own $id.
import { Ajv2020, type ValidateFunction } from "ajv/dist/2020.js";
import { readFileSync } from "node:fs";
import path from "node:path";
import { schemaDir } from "./paths.js";

export type SchemaName =
  | "bridge_request"
  | "bridge_response"
  | "decision_request"
  | "decision"
  | "order_intent"
  | "intent_result"
  | "decision_ledger_entry"
  // Protocol v1.1 additions (issues #34, #29): referenced by $ref from
  // bridge_request/bridge_response/intent_result, so ajv's strict-mode
  // schema compilation fails to resolve those refs unless these are loaded
  // too, even for a plain v1 (sim-mode) message.
  | "order_execution"
  | "approval"
  | "hello";

const SCHEMA_NAMES: readonly SchemaName[] = [
  "bridge_request",
  "bridge_response",
  "decision_request",
  "decision",
  "order_intent",
  "intent_result",
  "decision_ledger_entry",
  "order_execution",
  "approval",
  "hello",
];

let ajv: Ajv2020 | undefined;
const validators = new Map<SchemaName, ValidateFunction>();
const rawSchemas = new Map<SchemaName, Record<string, unknown>>();

function ensureLoaded(dir: string): void {
  if (ajv) return;
  // strictRequired is off: decision.schema.json and bridge_request.schema.json both use
  // `if/then.required` to require a property declared in the outer schema (e.g. "qty" only
  // when action is buy/sell) — valid JSON Schema 2020-12, but ajv's strict-mode linter (not
  // the spec) rejects a `required` name it can't find declared on that same `then` subschema.
  ajv = new Ajv2020({ strict: true, strictRequired: false, allErrors: true });
  for (const name of SCHEMA_NAMES) {
    const schema = JSON.parse(readFileSync(path.join(dir, `${name}.schema.json`), "utf8"));
    rawSchemas.set(name, schema);
    ajv.addSchema(schema);
  }
}

export function getValidator(name: SchemaName): ValidateFunction {
  ensureLoaded(schemaDir());
  const cached = validators.get(name);
  if (cached) return cached;
  // Looked up by the schema file's own $id, so the contract, not this file, owns the URI.
  const validate = ajv!.getSchema(String(rawSchemas.get(name)?.$id));
  if (!validate) throw new Error(`schema not loaded: ${name}`);
  validators.set(name, validate);
  return validate;
}

/** Throws with the ajv error list when `data` does not satisfy schema `name`. */
export function validateOrThrow(name: SchemaName, data: unknown): void {
  const validate = getValidator(name);
  if (!validate(data)) {
    throw new Error(`schema validation failed for ${name}: ${JSON.stringify(validate.errors)}`);
  }
}

/** The raw JSON Schema object, e.g. to pass as `outputFormat.schema` to the Agent SDK. */
export function loadRawSchema(name: SchemaName): Record<string, unknown> {
  ensureLoaded(schemaDir());
  const schema = rawSchemas.get(name);
  if (!schema) throw new Error(`schema not loaded: ${name}`);
  return schema;
}
