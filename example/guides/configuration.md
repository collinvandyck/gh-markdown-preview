# Configuration

| Key | Type | Default | Effect |
|---|---|---|---|
| `region` | string | none | Which harbor the light serves |
| `beam.rpm` | int | 6 | Rotations per minute |
| `lens.brightness` | int | 80 | Percent of maximum output |
| `lens.fog` | bool | false | Switches to the low, wide fog pattern |

Changes to `beam.rpm` take effect on the next rotation. Changes to `lens.*` take effect immediately.

> Fog mode doubles power draw. Check the [storage](../architecture/storage.md) keys if a setting doesn't seem to stick.
