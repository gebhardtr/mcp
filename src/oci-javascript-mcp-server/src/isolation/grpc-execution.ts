/*
 * Copyright (c) 2026, Oracle and/or its affiliates.
 * Licensed under the Universal Permissive License v1.0 as shown at
 * https://oss.oracle.com/licenses/upl.
 */

import type { ChildProcess } from "node:child_process";
import {
  compressionAlgorithms,
  status,
  type ChannelCredentials,
  type ClientDuplexStream
} from "@grpc/grpc-js";
import { RunnerClient, type ProtocolFrame } from "../grpc.ts";
import {
  DEFAULT_MAX_FRAME_BYTES,
  ProtocolError,
  assertExactFields,
  decodePayload,
  encodePayload,
  protocolMessage
} from "../protocol.ts";
import { appendCapped, MAX_STDERR_BYTES, MAX_STDOUT_BYTES } from "../sandbox-common.ts";
import type {
  IsolationExecution,
  IsolationRunOptions,
  Json,
  JsonObject,
  SandboxResult
} from "../types.ts";

const GRPC_OPTIONS = {
  "grpc.default_compression_algorithm": compressionAlgorithms.identity,
  "grpc.enable_retries": 0,
  "grpc.max_receive_message_length": DEFAULT_MAX_FRAME_BYTES,
  "grpc.max_send_message_length": DEFAULT_MAX_FRAME_BYTES,
  "grpc.ssl_target_name_override": "oci-javascript-runner"
} as const;

export function startGrpcExecution(
  child: ChildProcess,
  address: string,
  credentials: ChannelCredentials,
  code: string,
  input: IsolationRunOptions & { memoryLimitMb: number; maxResultBytes: number }
): IsolationExecution {
  const client = new RunnerClient(address, credentials, GRPC_OPTIONS);
  let call: ClientDuplexStream<ProtocolFrame, ProtocolFrame> | undefined;
  let stdout = "";
  let stderr = "";
  let ready = false;
  let settled = false;
  let workerCompleted = false;
  let closed = false;
  let resolveResult!: (value: SandboxResult) => void;
  const result = new Promise<SandboxResult>(resolve => {
    resolveResult = resolve;
  });
  const close = new Promise<void>(resolve => child.once("close", () => {
    closed = true;
    resolve();
  }));
  let cleanup: Promise<void> | undefined;
  const terminate = () => cleanup ??= (async () => {
    if (workerCompleted && !closed) {
      await waitForClose(close, 500);
    }
    call?.cancel();
    client.close();
    if (!closed) {
      killChildTree(child);
    }
    await close;
  })();

  const finish = (value: SandboxResult, stop = false) => {
    if (settled) {
      return;
    }
    settled = true;
    clearTimeout(timeout);
    input.signal.removeEventListener("abort", abort);
    resolveResult({ ...value, stdout, stderr });
    if (stop) {
      void terminate();
    }
  };
  const fail = (message: string, stop = true) => finish({
    result: null,
    error: { message },
    stdout: "",
    stderr: "",
    exitCode: 1,
    timedOut: false
  }, stop);

  client.waitForReady(input.deadlineMs, error => {
    if (error) {
      finish(timeoutResult(), true);
      return;
    }
    if (settled) {
      return;
    }
    call = client.session({ deadline: input.deadlineMs });
    call.on("data", frame => {
      try {
        const message = decodePayload(frame.payload);
        void handleWorkerMessage(message, call!, input, {
          ready() {
            if (ready) {
              throw new ProtocolError("sandbox worker sent duplicate health message");
            }
            ready = true;
            send(call!, "execute", {
              code,
              timeoutMs: Math.max(1, input.deadlineMs - Date.now()),
              reflectionManifest: input.reflectionManifest ?? { services: {} },
              memoryLimitMb: input.memoryLimitMb,
              maxResultBytes: input.maxResultBytes
            });
          },
          appendLog(stream, text) {
            if (stream === "stdout") {
              stdout = appendCapped(stdout, text, MAX_STDOUT_BYTES);
            } else {
              stderr = appendCapped(stderr, text, MAX_STDERR_BYTES);
            }
          },
          finish(value, stop) {
            workerCompleted = true;
            finish(value, stop);
          }
        }).catch(() => fail("sandbox protocol failed"));
      } catch {
        fail("sandbox protocol failed");
      }
    });
    call.once("error", error => {
      if (error.code === status.DEADLINE_EXCEEDED) {
        finish(timeoutResult(), true);
      } else {
        fail("sandbox protocol failed");
      }
    });
    call.once("end", () => fail("sandbox protocol failed"));
  });
  child.once("error", () => fail("sandbox runner failed", false));
  child.once("close", (exitCode, signal) => {
    if (!settled) {
      fail(
        `sandbox runner exited before returning a result (${signal ?? exitCode ?? "unknown"})`,
        false
      );
    }
  });

  const timeout = setTimeout(() => finish(timeoutResult(), true), Math.max(
    1,
    input.deadlineMs - Date.now()
  ));
  timeout.unref();
  const abort = () => {
    try {
      if (call) {
        send(call, "cancel");
      }
    } catch {
      // Forced teardown below remains authoritative.
    }
    finish(timeoutResult(), true);
  };
  input.signal.addEventListener("abort", abort, { once: true });

  return { result, terminate };
}

async function handleWorkerMessage(
  message: JsonObject,
  call: ClientDuplexStream<ProtocolFrame, ProtocolFrame>,
  input: IsolationRunOptions,
  callbacks: {
    ready(): void;
    appendLog(stream: "stdout" | "stderr", text: string): void;
    finish(result: SandboxResult, stop?: boolean): void;
  }
): Promise<void> {
  if (message.type === "health") {
    assertExactFields(message, ["version", "type", "status"]);
    if (message.status !== "ready") {
      throw new ProtocolError("invalid sandbox worker health status");
    }
    callbacks.ready();
    return;
  }
  if (message.type === "log") {
    assertExactFields(message, ["version", "type", "stream", "text"]);
    if (
      (message.stream !== "stdout" && message.stream !== "stderr")
      || typeof message.text !== "string"
    ) {
      throw new ProtocolError("invalid sandbox log message");
    }
    callbacks.appendLog(message.stream, message.text);
    return;
  }
  if (message.type === "rpc") {
    assertExactFields(message, ["version", "type", "id", "request"]);
    if (!Number.isInteger(message.id) || !isObject(message.request)) {
      throw new ProtocolError("invalid sandbox RPC message");
    }
    let rpcResult: Json;
    try {
      rpcResult = await input.hostRpc(copyToPlainJson(message.request) as JsonObject);
    } catch {
      rpcResult = { ok: false, error: { message: "OCI call failed" } };
    }
    send(call, "rpc_result", { id: message.id, result: rpcResult });
    return;
  }
  if (message.type === "result") {
    assertExactFields(message, ["version", "type", "result", "error", "exitCode", "timedOut"]);
    if (
      !Number.isInteger(message.exitCode)
      || typeof message.timedOut !== "boolean"
      || (message.error !== null && !isObject(message.error))
    ) {
      throw new ProtocolError("invalid sandbox result message");
    }
    callbacks.finish({
      result: message.result ?? null,
      error: message.error as SandboxResult["error"],
      stdout: "",
      stderr: "",
      exitCode: message.exitCode as number,
      timedOut: message.timedOut as boolean
    });
    return;
  }
  if (message.type === "protocol_error") {
    assertExactFields(message, ["version", "type", "error"]);
    throw new ProtocolError("sandbox worker reported a protocol failure");
  }
  throw new ProtocolError(`unsupported sandbox message type '${String(message.type)}'`);
}

function send(
  call: ClientDuplexStream<ProtocolFrame, ProtocolFrame>,
  type: string,
  fields: JsonObject = {}
): void {
  if (call.destroyed || call.writableEnded) {
    throw new Error("sandbox runner channel is closed");
  }
  call.write({ payload: encodePayload(protocolMessage(type, fields)) });
}

function killChildTree(child: ChildProcess): void {
  if (child.exitCode !== null || child.signalCode !== null) {
    return;
  }
  if (process.platform !== "win32" && child.pid) {
    try {
      process.kill(-child.pid, "SIGKILL");
      return;
    } catch {
      // Fall back to killing the direct child.
    }
  }
  child.kill("SIGKILL");
}

function timeoutResult(): SandboxResult {
  return {
    result: null,
    error: { message: "sandbox run deadline exceeded" },
    stdout: "",
    stderr: "",
    exitCode: -1,
    timedOut: true
  };
}

function waitForClose(close: Promise<void>, timeoutMs: number): Promise<void> {
  return new Promise(resolve => {
    const timeout = setTimeout(resolve, timeoutMs);
    timeout.unref();
    close.finally(() => {
      clearTimeout(timeout);
      resolve();
    });
  });
}

function isObject(value: Json | undefined): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function copyToPlainJson(value: Json): Json {
  if (Array.isArray(value)) {
    return value.map(copyToPlainJson);
  }
  if (!isObject(value)) {
    return value;
  }
  const result: JsonObject = {};
  for (const [key, child] of Object.entries(value)) {
    result[key] = copyToPlainJson(child);
  }
  return result;
}
