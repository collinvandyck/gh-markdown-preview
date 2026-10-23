# Request Flow

This page follows a `rotate` request from the client to durable storage.

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant G as Gateway
    participant B as Beam
    participant K as Keeper

    C->>G: POST /v1/beam/rotate
    G->>G: verify token
    G->>B: Rotate(angle)
    B->>K: Put(beam/angle)
    K-->>B: ok
    B-->>G: ok
    G-->>C: 204 No Content
```

## Failure handling

If Keeper is unavailable, Beam retries with exponential backoff for up to 30 seconds and then returns `503`. The gateway does not retry on its own, so a client sees at most one `503` per request.

See [Storage](./storage.md#write-path) for what Keeper does with the write.
