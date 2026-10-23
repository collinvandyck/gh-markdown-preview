# Storage

Keeper stores state as versioned keys.

## Write path

1. Beam or Lens sends `Put(key, value)`.
2. Keeper appends the write to its log.
3. Keeper applies the write to its in-memory index and acknowledges.

## Keys

| Key | Type | Written by |
|---|---|---|
| `beam/angle` | float | Beam |
| `lens/brightness` | int, 0 to 100 | Lens |
| `lens/fog` | bool | Lens |
