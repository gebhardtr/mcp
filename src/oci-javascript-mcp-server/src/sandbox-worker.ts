#!/usr/bin/env -S node --no-node-snapshot --experimental-strip-types
/*
 * Copyright (c) 2026, Oracle and/or its affiliates.
 * Licensed under the Universal Permissive License v1.0 as shown at
 * https://oss.oracle.com/licenses/upl.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  Server,
  ServerCredentials,
  type ServerDuplexStream
} from "@grpc/grpc-js";
import { RUNNER_SERVICE, type ProtocolFrame } from "./grpc.ts";
import {
  DEFAULT_DECODE_LIMITS,
  ProtocolError,
  assertExactFields,
  decodePayload,
  encodePayload,
  protocolMessage
} from "./protocol.ts";
import { runJavaScriptInIsolate } from "./sandbox-isolate.ts";
import type { Json, JsonObject, OciReflectionManifest } from "./types.ts";

type PendingRpc = {
  resolve: (value: Json) => void;
  reject: (error: Error) => void;
};

// The execute frame contains a trusted, host-generated SDK reflection manifest.
// Frames emitted by sandbox code are decoded by the host with tighter defaults.
const decodeLimits = {
  ...DEFAULT_DECODE_LIMITS,
  maxObjectKeys: 100_000,
  maxNodes: 250_000
};
const pendingRpc = new Map<number, PendingRpc>();
let nextRpcId = 1;
let running = false;
let session: ServerDuplexStream<ProtocolFrame, ProtocolFrame> | undefined;

const server = new Server({
  "grpc.max_receive_message_length": DEFAULT_DECODE_LIMITS.maxFrameBytes,
  "grpc.max_send_message_length": DEFAULT_DECODE_LIMITS.maxFrameBytes
});
server.addService(RUNNER_SERVICE, { session: openSession });
void startServer().catch(fatal);

function openSession(call: ServerDuplexStream<ProtocolFrame, ProtocolFrame>): void {
  if (session) {
    call.end();
    return;
  }
  session = call;
  call.on("data", frame => {
    try {
      void handleMessage(decodePayload(frame.payload, decodeLimits)).catch(fatal);
    } catch (error) {
      fatal(error);
    }
  });
  call.on("error", fatal);
  call.on("cancelled", () => {
    rejectPending(new Error("sandbox host channel closed"));
    process.exit(1);
  });
  send("health", { status: "ready" });
}

async function startServer(): Promise<void> {
  const tls = await loadTls();
  const credentials = ServerCredentials.createSsl(
    Buffer.from(tls.clientCert),
    [{
      private_key: Buffer.from(tls.serverKey),
      cert_chain: Buffer.from(tls.serverCert)
    }],
    true
  );
  const port = Number(process.env.OCI_JAVASCRIPT_RUNNER_PORT ?? 50051);
  if (!isPositiveInteger(port) || port > 65535) {
    throw new Error("invalid sandbox runner port");
  }
  await new Promise<void>((resolve, reject) => {
    server.bindAsync(`0.0.0.0:${port}`, credentials, error => {
      if (error) {
        reject(error);
      } else {
        resolve();
      }
    });
  });
}

async function loadTls(): Promise<{
  serverKey: string;
  serverCert: string;
  clientCert: string;
}> {
  if (process.env.OCI_JAVASCRIPT_RUNNER_TLS_STDIN === "1") {
    let input = "";
    for await (const chunk of process.stdin) {
      input += String(chunk);
      if (Buffer.byteLength(input, "utf8") > 32 * 1024) {
        throw new Error("sandbox TLS bootstrap is too large");
      }
    }
    const value = JSON.parse(input) as unknown;
    if (!isTlsBootstrap(value)) {
      throw new Error("invalid sandbox TLS bootstrap");
    }
    return value;
  }
  const directory = process.env.OCI_JAVASCRIPT_RUNNER_TLS_DIR ?? "/run/oci-runner";
  return {
    serverKey: readFileSync(join(directory, "server.key"), "utf8"),
    serverCert: readFileSync(join(directory, "server.crt"), "utf8"),
    clientCert: readFileSync(join(directory, "client.crt"), "utf8")
  };
}

async function handleMessage(message: JsonObject): Promise<void> {
  if (message.type === "execute") {
    assertExactFields(message, [
      "version",
      "type",
      "code",
      "timeoutMs",
      "reflectionManifest",
      "memoryLimitMb",
      "maxResultBytes"
    ]);
    if (running) {
      throw new ProtocolError("sandbox worker accepts exactly one execution");
    }
    if (
      typeof message.code !== "string"
      || !isPositiveInteger(message.timeoutMs)
      || !isObject(message.reflectionManifest)
      || !isPositiveInteger(message.memoryLimitMb)
      || !isPositiveInteger(message.maxResultBytes)
    ) {
      throw new ProtocolError("invalid sandbox execute message");
    }
    running = true;
    await execute(
      message.code,
      message.timeoutMs,
      message.reflectionManifest as unknown as OciReflectionManifest,
      message.memoryLimitMb,
      message.maxResultBytes
    );
    return;
  }

  if (message.type === "rpc_result") {
    assertExactFields(message, ["version", "type", "id", "result"]);
    if (!Number.isInteger(message.id)) {
      throw new ProtocolError("invalid sandbox RPC result");
    }
    const pending = pendingRpc.get(message.id as number);
    if (!pending) {
      throw new ProtocolError("unknown sandbox RPC response id");
    }
    pendingRpc.delete(message.id as number);
    pending.resolve(message.result ?? null);
    return;
  }

  if (message.type === "cancel") {
    assertExactFields(message, ["version", "type"]);
    rejectPending(new Error("sandbox execution cancelled"));
    process.exit(124);
  }

  throw new ProtocolError(`unsupported host message type '${String(message.type)}'`);
}

async function execute(
  code: string,
  timeoutMs: number,
  reflectionManifest: OciReflectionManifest,
  memoryLimitMb: number,
  maxResultBytes: number
): Promise<void> {
  const result = await runJavaScriptInIsolate(code, {
    timeoutSeconds: timeoutMs / 1000,
    hostRpc,
    reflectionManifest,
    memoryLimitMb,
    maxResultBytes
  });
  if (result.stdout) {
    send("log", { stream: "stdout", text: result.stdout });
  }
  if (result.stderr) {
    send("log", { stream: "stderr", text: result.stderr });
  }
  sendAndExit("result", {
    result: result.result,
    error: result.error,
    exitCode: result.exitCode,
    timedOut: result.timedOut
  }, result.exitCode === 0 ? 0 : 1);
}

function hostRpc(request: unknown): Promise<Json> {
  const id = nextRpcId;
  nextRpcId += 1;
  return new Promise((resolve, reject) => {
    pendingRpc.set(id, { resolve, reject });
    try {
      send("rpc", {
        id,
        request: request as Json
      });
    } catch (error) {
      pendingRpc.delete(id);
      reject(error instanceof Error ? error : new Error(String(error)));
    }
  });
}

function send(type: string, fields: JsonObject = {}): void {
  if (!session || session.destroyed || session.writableEnded) {
    throw new Error("sandbox host channel is closed");
  }
  session.write({ payload: encodePayload(protocolMessage(type, fields)) });
}

function sendAndExit(type: string, fields: JsonObject, exitCode: number): void {
  if (!session) {
    process.exit(exitCode);
  }
  session.write({ payload: encodePayload(protocolMessage(type, fields)) }, () => {
    session?.end();
    server.tryShutdown(() => process.exit(exitCode));
  });
}

function fatal(_error: unknown): void {
  try {
    send("protocol_error", { error: { message: "sandbox protocol failure" } });
  } catch {
    // The channel may already be unusable.
  }
  rejectPending(new Error("sandbox protocol failure"));
  process.exit(70);
}

function rejectPending(error: Error): void {
  for (const pending of pendingRpc.values()) {
    pending.reject(error);
  }
  pendingRpc.clear();
}

function isPositiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value > 0;
}

function isObject(value: unknown): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isTlsBootstrap(value: unknown): value is {
  serverKey: string;
  serverCert: string;
  clientCert: string;
} {
  return isObject(value)
    && Object.keys(value).length === 3
    && typeof value.serverKey === "string"
    && typeof value.serverCert === "string"
    && typeof value.clientCert === "string";
}
