import assert from "node:assert/strict";
import http from "node:http";
import { fileURLToPath } from "node:url";

import { createServer } from "vite";

const backendOrigin = "http://127.0.0.1:8787";
const backendHost = "127.0.0.1:8787";
const frontendRoot = fileURLToPath(new URL("..", import.meta.url));
const configFile = fileURLToPath(new URL("../vite.config.ts", import.meta.url));

function listen(server, port) {
  return new Promise((resolve, reject) => {
    const onError = (error) => {
      server.off("listening", onListening);
      reject(error);
    };
    const onListening = () => {
      server.off("error", onError);
      resolve();
    };
    server.once("error", onError);
    server.once("listening", onListening);
    server.listen(port, "127.0.0.1");
  });
}

async function close(server) {
  if (!server.listening) return;
  await new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
    server.closeAllConnections?.();
  });
}

async function reservePort() {
  const probe = http.createServer();
  await listen(probe, 0);
  const address = probe.address();
  assert(address && typeof address === "object", "failed to reserve a Vite port");
  const port = address.port;
  await close(probe);
  return port;
}

function browserCanReadCors(response, requestOrigin) {
  const allowedOrigin = response.headers.get("access-control-allow-origin");
  return allowedOrigin === "*" || allowedOrigin === requestOrigin;
}

const backendRequests = [];
const backend = http.createServer((request, response) => {
  const received = {
    method: request.method,
    url: request.url,
    origin: request.headers.origin ?? null,
    host: request.headers.host ?? null
  };
  backendRequests.push(received);

  if (request.method !== "GET" || request.url !== "/api/session") {
    response.writeHead(404).end();
    return;
  }

  response.setHeader("Content-Type", "application/json; charset=utf-8");
  response.end(JSON.stringify({ ok: true, received }));
});

let vite;
try {
  await listen(backend, 8787);
  const port = await reservePort();
  vite = await createServer({
    root: frontendRoot,
    configFile,
    logLevel: "silent",
    server: {
      host: "127.0.0.1",
      port,
      strictPort: true
    }
  });
  await vite.listen();

  assert.equal(vite.config.server.cors, false, "dev server CORS must be explicitly disabled");
  assert.equal(vite.config.preview.cors, false, "preview server CORS must be explicitly disabled");

  const address = vite.httpServer?.address();
  assert(address && typeof address === "object", "Vite must expose its listening address");
  assert.equal(address.port, port, "Vite must bind the reserved integration port");
  const devOrigin = `http://127.0.0.1:${address.port}`;
  const siblingOrigin = `http://localhost:${address.port}`;
  const siblingResponse = await fetch(`${devOrigin}/api/session`, {
    headers: { Origin: siblingOrigin }
  });
  assert.equal(siblingResponse.status, 200, "proxy request should reach the mock backend");
  const siblingAccessControlAllowOrigin = siblingResponse.headers.get("access-control-allow-origin");
  assert.equal(
    siblingAccessControlAllowOrigin,
    null,
    "sibling localhost origin must not receive Access-Control-Allow-Origin"
  );
  assert.equal(
    browserCanReadCors(siblingResponse, siblingOrigin),
    false,
    "sibling localhost origin must not become browser-readable CORS"
  );
  const siblingBody = await siblingResponse.json();

  const sameOriginResponse = await fetch(`${devOrigin}/api/session`, {
    headers: { Origin: devOrigin }
  });
  assert.equal(sameOriginResponse.status, 200, "same-origin dev request should remain usable");
  const sameOriginBody = await sameOriginResponse.json();

  for (const body of [siblingBody, sameOriginBody]) {
    assert.equal(body.received.origin, backendOrigin, "proxy must rewrite Origin to the backend origin");
    assert.equal(body.received.host, backendHost, "proxy must rewrite Host to the backend host");
  }
  assert.equal(backendRequests.length, 2, "mock backend should receive exactly the two test requests");

  console.log(
    JSON.stringify({
      status: "PASS",
      siblingOrigin,
      siblingAccessControlAllowOrigin,
      sameOriginStatus: sameOriginResponse.status,
      backendReadback: sameOriginBody.received
    })
  );
} finally {
  if (vite) await vite.close();
  await close(backend);
}
