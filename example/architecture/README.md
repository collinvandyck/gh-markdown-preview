# Architecture

Lighthouse has one stateless edge tier and one stateful core. The edge accepts requests and the core decides what the light does.

- [Request flow](./request-flow.md) walks one request through every service.
- [Storage](./storage.md) covers how Keeper persists state.

```mermaid
flowchart TD
    subgraph Edge
        Gateway["Gateway"]
    end
    subgraph Core
        Beam["Beam"]
        Lens["Lens"]
        Keeper[("Keeper")]
    end
    Gateway --> Beam
    Gateway --> Lens
    Beam --> Keeper
    Lens --> Keeper
```
