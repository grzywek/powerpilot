# PowerPilot brand icon

Source and rendered brand icon for the integration.

| File | Size | Use |
|------|------|-----|
| `icon.svg` | vector | editable source |
| `icon.png` | 256×256 | Home Assistant brands `icon.png` |
| `icon@2x.png` | 512×512 | Home Assistant brands `icon@2x.png` |

The mark: a blue house outline containing a charging battery (yellow bolt +
green charge bars) and a power plug, wrapped in green refresh-cycle arrows and a
green upward trend arrow — home energy being optimised under a dynamic tariff.
Transparent background, no tile.

## Re-rendering after editing `icon.svg`

```bash
cd assets/brand
rsvg-convert -w 256 -h 256 icon.svg -o icon.png
rsvg-convert -w 512 -h 512 icon.svg -o icon@2x.png
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
