# Getting Started

## Install

```sh
brew install lighthouse   # not a real formula
```

## Configure

Create `lighthouse.json`:

```json
{
  "region": "harbor-1",
  "beam": { "rpm": 6 },
  "lens": { "brightness": 80, "fog": false }
}
```

## Checklist

- [x] Install the CLI
- [x] Write `lighthouse.json`
- [ ] Run `lighthouse serve`
- [ ] Point a client at port 8080

Next: [Configuration](./configuration.md).
