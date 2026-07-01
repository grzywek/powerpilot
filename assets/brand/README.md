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

Home Assistant serves integration icons from the
[`home-assistant/brands`](https://github.com/home-assistant/brands) repository,
not from this repo. To make the icon appear next to **PowerPilot** in
*Settings → Devices & Services* and in HACS, open a PR to that repo adding:

```
custom_integrations/powerpilot/icon.png      (this icon.png)
custom_integrations/powerpilot/icon@2x.png   (this icon@2x.png)
```

The sidebar panel keeps using `mdi:home-battery` (set in `panel.py`); the
built-in custom panel takes an MDI name, not a bitmap.
