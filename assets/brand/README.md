# PowerPilot brand icon

Master and rendered brand icon for the integration.

| File | Size | Use |
|------|------|-----|
| `icon-source.png` | 960×960 | master artwork |
| `icon.png` | 256×256 | Home Assistant brands `icon.png` |
| `icon@2x.png` | 512×512 | Home Assistant brands `icon@2x.png` |

The mark: a blue house outline containing a charging battery (yellow bolt +
green charge bars) and a power plug, wrapped in green refresh-cycle arrows and a
green upward trend arrow — home energy being optimised under a dynamic tariff.
Transparent background, no tile.

## Re-rendering from the master

```bash
cd assets/brand
magick icon-source.png -filter Lanczos -resize 256x256 -strip icon.png
magick icon-source.png -filter Lanczos -resize 512x512 -strip icon@2x.png
```

## Showing it on the HACS / integrations page

Home Assistant 2026.3 and newer loads custom integration brand images from a
local `brand/` directory inside the integration:

```
custom_components/powerpilot/brand/icon.png      (this icon.png)
custom_components/powerpilot/brand/icon@2x.png   (this icon@2x.png)
```

Those files are packaged with the custom component so the icon appears next to
**PowerPilot** in *Settings → Devices & Services* without submitting a separate
brands repository PR.

The sidebar panel keeps using `mdi:home-battery` (set in `panel.py`); the
built-in custom panel takes an MDI name, not a bitmap.
