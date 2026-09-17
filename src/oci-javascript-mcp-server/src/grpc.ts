/*
 * Copyright (c) 2026, Oracle and/or its affiliates.
 * Licensed under the Universal Permissive License v1.0 as shown at
 * https://oss.oracle.com/licenses/upl.
 */

import {
  Client,
  makeGenericClientConstructor,
  type CallOptions,
  type ChannelCredentials,
  type ClientDuplexStream,
  type ClientOptions,
  type ServiceDefinition
} from "@grpc/grpc-js";
import { DEFAULT_MAX_FRAME_BYTES, ProtocolError } from "./protocol.ts";

export type ProtocolFrame = { payload: Buffer };

const codec = {
  serialize(frame: ProtocolFrame): Buffer {
    if (!Buffer.isBuffer(frame.payload) || frame.payload.length > DEFAULT_MAX_FRAME_BYTES) {
      throw new ProtocolError("invalid gRPC protocol frame");
    }
    return frame.payload;
  },
  deserialize(payload: Buffer): ProtocolFrame {
    if (payload.length > DEFAULT_MAX_FRAME_BYTES) {
      throw new ProtocolError("invalid gRPC protocol frame");
    }
    return { payload };
  }
};

export const RUNNER_SERVICE: ServiceDefinition = {
  session: {
    path: "/oracle.oci.mcp.runner.v1.Runner/Session",
    requestStream: true,
    responseStream: true,
    requestSerialize: codec.serialize,
    requestDeserialize: codec.deserialize,
    responseSerialize: codec.serialize,
    responseDeserialize: codec.deserialize
  }
};

export interface RunnerClient extends Client {
  session(options?: CallOptions): ClientDuplexStream<ProtocolFrame, ProtocolFrame>;
}

export const RunnerClient = makeGenericClientConstructor(
  RUNNER_SERVICE,
  "oracle.oci.mcp.runner.v1.Runner"
) as unknown as {
  new(address: string, credentials: ChannelCredentials, options?: ClientOptions): RunnerClient;
};
