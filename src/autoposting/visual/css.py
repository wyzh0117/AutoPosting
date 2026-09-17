from __future__ import annotations

from .tokens import flatten_tokens, load_tokens


def stylesheet(tokens: dict | None = None) -> str:
    tokens = tokens or load_tokens()
    variables = "\n".join(
        f"    --{name}: {value};" for name, value in flatten_tokens(tokens).items()
    )
    canvas = tokens["canvas"]
    safe = tokens["safeArea"]
    color = tokens["color"]
    # Weekly geometric templates use a compact three-color vocabulary. Keep
    # their layers derived from resolved tokens so the picker changes pixels.
    template_main = str(color["background"]).lower()
    template_secondary = str(color["surface"]).lower()
    template_accent = str(color["primary"]).lower()
    template_ink = str(color["text"]["primary"]).lower()
    template_muted = str(color["text"]["secondary"]).lower()
    template_border = str(color["border"]).lower()
    template_surface_raised = str(color["surfaceRaised"]).lower()
    type_ = tokens["typography"]
    space = tokens["spacing"]
    radius = tokens["radius"]
    return f"""
:root {{
{variables}
}}
* {{ box-sizing: border-box; }}
html, body {{
  margin: 0;
  padding: 0;
  width: {canvas["width"]}px;
  height: {canvas["height"]}px;
  overflow: hidden;
  background: {color["background"]};
  color: {color["text"]["primary"]};
  font-family: {type_["fontFamily"]["cjk"]};
  -webkit-font-smoothing: antialiased;
}}
.page {{
  position: relative;
  width: {canvas["width"]}px;
  height: {canvas["height"]}px;
  padding: {safe["top"]}px {safe["right"]}px {safe["bottom"]}px {safe["left"]}px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background:
    linear-gradient(180deg, {color.get("wash") or color["primaryMuted"]}, transparent 28%),
    {color["background"]};
}}
.page::before {{
  content: "";
  position: absolute;
  inset: 0;
  background-image:
    linear-gradient({color["backgroundGrid"]} 1px, transparent 1px),
    linear-gradient(90deg, {color["backgroundGrid"]} 1px, transparent 1px);
  background-size: 48px 48px;
  mask-image: radial-gradient(ellipse at top, #000 40%, transparent 78%);
  pointer-events: none;
}}
.template-cover::after,
.template-poster-a::after,
.template-poster-b::after {{
  content: "";
  position: absolute;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  background: {template_main};
}}
.template-cover::after {{
  background:
    linear-gradient(134deg, transparent 0 63%, {template_secondary} 63% 100%),
    linear-gradient(180deg, {template_secondary} 0 8%, transparent 8% 100%),
    {template_main};
  clip-path: polygon(0 0, 100% 0, 100% 100%, 26% 100%, 0 75%);
}}
.template-poster-a::after {{
  background:
    linear-gradient(135deg, transparent 0 27%, {template_surface_raised} 27% 47%, transparent 47% 100%),
    linear-gradient(90deg, transparent 0 78%, {template_surface_raised} 78% 100%),
    {template_main};
}}
.template-poster-b::after {{
  background:
    linear-gradient(180deg, {template_secondary} 0 12%, transparent 12% 100%),
    linear-gradient(147deg, transparent 0 62%, {template_secondary} 62% 100%),
    {template_main};
}}
.template-cover::before,
.template-poster-a::before,
.template-poster-b::before {{
  background-image:
    linear-gradient(rgba(112,128,185,.12) 1px, transparent 1px),
    linear-gradient(90deg, rgba(112,128,185,.12) 1px, transparent 1px);
  background-size: 72px 72px;
  mask-image: linear-gradient(180deg, #000 0 62%, transparent 90%);
  opacity: .35;
}}
.template-cover > .c-brand-header,
.template-cover > .c-hero-title,
.template-cover > .c-meta-row,
.template-cover > .c-ranking-list,
.template-cover > .c-topic-tags,
.template-cover > .c-footer,
.template-poster-a > *,
.template-poster-b > * {{ z-index: 1; }}
.template-cover,
.template-poster-a,
.template-poster-b {{ border: 1px solid rgba(112,128,185,.46); }}
.template-poster-a.page-project,
.template-poster-b.page-project {{
  padding: {safe["top"]}px {safe["right"]}px {safe["bottom"]}px {safe["left"]}px;
}}
.template-poster-a.page-project .c-project-shell,
.template-poster-b.page-project .c-project-shell {{ padding: 0; }}
.template-poster-a .c-ranking-item,
.template-poster-b .c-ranking-item,
.template-poster-a .c-panel,
.template-poster-b .c-panel {{
  border-color: rgba(112,128,185,.82);
  border-radius: 0;
  background: rgba(16,31,99,.82);
}}
.template-poster-a .c-ranking-item,
.template-poster-b .c-ranking-item {{ box-shadow: none; }}
.page > * {{ position: relative; z-index: 1; }}
.c-brand-header {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 36px;
}}
.c-brand-mark {{
  display: flex;
  align-items: center;
  gap: {space["sm"]}px;
  font-size: {type_["caption"]["size"]}px;
  font-weight: {tokens["fontWeight"]["semibold"]};
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: {color["text"]["secondary"]};
}}
.c-brand-mark i {{
  width: 14px;
  height: 14px;
  border-radius: 4px;
  background: {color["primary"]};
  box-shadow: {tokens["glow"]["accent"]};
  display: block;
}}
.c-brand-author {{
  text-transform: none;
  letter-spacing: 0.04em;
  font-size: 15px;
  color: {color["text"]["primary"]};
}}
.c-brand-avatar {{
  width: 32px;
  height: 32px;
  border-radius: 50%;
  object-fit: cover;
  background: #fff;
  box-shadow: {tokens["glow"]["accent"]};
  flex: 0 0 auto;
}}
.c-issue-badge, .c-category-tag, .c-page-number {{
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  border: 1px solid {color["border"]};
  border-radius: {radius["pill"]}px;
  color: {color["text"]["secondary"]};
  font-size: {type_["caption"]["size"]}px;
  letter-spacing: 0.08em;
}}
.c-issue-badge {{
  text-transform: none;
  letter-spacing: 0.04em;
}}
.c-page-number {{
  text-transform: uppercase;
}}
.c-category-tag {{
  letter-spacing: 0.04em;
}}
.c-hero-title {{
  margin: {space["lg"]}px 0 0;
  font-family: {type_["fontFamily"]["latin"]};
  font-size: {type_["h1"]["size"]}px;
  font-weight: {type_["h1"]["weight"]};
  line-height: {type_["h1"]["lineHeight"]};
  letter-spacing: {type_["h1"]["letterSpacing"]};
  max-height: calc({type_["h1"]["size"]}px * {type_["h1"]["lineHeight"]} * 2);
  overflow: hidden;
}}
.page-ranking .c-hero-title {{
  font-size: 64px;
  margin-top: {space["sm"]}px;
  max-height: calc(64px * {type_["h1"]["lineHeight"]} * 2);
}}
.c-masthead .c-hero-title {{
  max-height: calc({type_["h2"]["size"]}px * {type_["h2"]["lineHeight"]} * 2);
}}
.c-masthead .c-quote-card {{ margin-top: {space["sm"]}px; }}
.c-masthead .c-quote-lede {{ max-width: none; }}
.c-hero-sub {{
  margin: {space["sm"]}px 0 0;
  font-size: {type_["subtitle"]["size"]}px;
  font-weight: {type_["subtitle"]["weight"]};
  color: {color["text"]["secondary"]};
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}}
.c-meta-row {{
  margin-top: {space["md"]}px;
  display: flex;
  gap: {space["md"]}px;
  color: {color["text"]["muted"]};
  font-size: {type_["caption"]["size"]}px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}}
.c-ranking-list {{
  flex: 1;
  min-height: 0;
  margin-top: {space["md"]}px;
  display: flex;
  flex-direction: column;
  gap: {space["xs"]}px;
}}
.c-ranking-item {{
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 56px 64px minmax(0, 1fr) 108px;
  align-items: center;
  gap: {space["md"]}px;
  padding: 0 {space["md"]}px;
  background: {color["surface"]};
  border: 1px solid {color["border"]};
  border-radius: {radius["card"]}px;
  box-shadow: {tokens["shadow"]["card"]};
}}
.c-ranking-badge {{
  font-family: {type_["fontFamily"]["latin"]};
  font-size: {type_["rank"]["size"]}px;
  font-weight: {type_["rank"]["weight"]};
  letter-spacing: {type_["rank"]["letterSpacing"]};
  color: {color["rank"]};
}}
.c-ranking-item:nth-child(n+4) .c-ranking-badge {{ color: {color["rankMuted"]}; }}
.c-project-logo {{
  width: 56px;
  height: 56px;
  border-radius: {radius["logo"]}px;
  border: 1px solid {color["border"]};
  background: {color["primaryMuted"]};
  color: {color["primary"]};
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  font-family: {type_["fontFamily"]["latin"]};
  font-weight: 700;
  font-size: 22px;
}}
.c-project-logo img {{ width: 100%; height: 100%; object-fit: contain; }}
.c-item-copy {{ min-width: 0; }}
.c-item-name {{
  font-family: {type_["fontFamily"]["latin"]};
  font-size: 22px;
  font-weight: 600;
  letter-spacing: -0.02em;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}}
.c-item-desc {{
  margin-top: 4px;
  font-size: 15px;
  color: {color["text"]["secondary"]};
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}}
.c-metric {{
  text-align: right;
}}
.c-metric-value {{
  font-family: {type_["fontFamily"]["latin"]};
  font-size: {type_["metric"]["size"]}px;
  font-weight: {type_["metric"]["weight"]};
  color: {color["metric"]};
}}
.c-metric-label {{
  margin-top: 2px;
  font-size: 11px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: {color["text"]["muted"]};
}}
.c-progress-bar {{
  margin-top: 8px;
  width: 100%;
  height: 4px;
  border-radius: 999px;
  background: {color["progressTrack"]};
  overflow: hidden;
}}
.c-progress-bar > span {{
  display: block;
  height: 100%;
  border-radius: 999px;
  background: {color["progressFill"]};
}}
.c-topic-tags {{
  display: flex;
  flex-wrap: nowrap;
  gap: {space["xs"]}px;
  margin-top: {space["md"]}px;
  overflow: hidden;
}}
.c-topic-tags span {{
  flex: 0 0 auto;
  padding: 6px 12px;
  border-radius: {radius["pill"]}px;
  border: 1px solid {color["border"]};
  color: {color["text"]["secondary"]};
  font-size: 13px;
}}
.c-footer {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: {space["md"]}px;
  color: {color["text"]["muted"]};
  font-size: 13px;
  letter-spacing: 0.04em;
}}
.page-project .c-hero-title {{ font-size: {type_["h2"]["size"] - 8}px; margin-top: {space["xs"]}px; }}
.c-project-shell {{
  flex: 1;
  min-height: 0;
  margin-top: {space["md"]}px;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: {space["md"]}px;
}}
.c-bullet-panel {{
  flex: 0 1 auto;
  height: auto;
  padding: {space["md"]}px {space["lg"]}px;
}}
.c-bullet-panel h3 {{
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: {space["sm"]}px;
}}
.c-bullet-panel h3 .c-feature-icon {{
  width: 22px;
  height: 22px;
  flex: 0 0 22px;
}}
.c-bullet-panel .c-feature-list {{
  gap: 8px;
}}
.c-bullet-panel .c-feature-list li {{
  align-items: flex-start;
  font-size: {type_["body"]["size"]}px;
  line-height: 1.4;
}}
.c-bullet-panel .c-feature-list li span {{
  white-space: normal;
}}
.c-plain-list .c-feature-list li {{
  display: block;
  gap: 0;
}}
.c-plain-list .c-feature-list li span {{
  display: block;
}}
.c-project-pair {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: {space["sm"]}px;
  flex: 0 1 auto;
  align-items: start;
}}
.page-project .c-panel h3 {{
  letter-spacing: 0.04em;
  text-transform: none;
  font-size: {type_["caption"]["size"]}px;
  color: {color["primary"]};
}}
.c-masthead {{
  display: grid;
  grid-template-columns: minmax(0, 1fr) 120px;
  gap: {space["md"]}px;
  align-items: start;
  margin-top: 0;
}}
.c-masthead-copy {{ min-width: 0; display: flex; flex-direction: column; }}
.c-masthead-logo {{
  width: 120px;
  height: 120px;
  aspect-ratio: 1 / 1;
  align-self: start;
}}
.c-masthead-logo .c-project-logo {{
  width: 120px;
  height: 120px;
  border: none;
  background: transparent;
  border-radius: 28px;
  box-shadow: none;
  font-size: 0;
}}
.c-masthead-logo img, .c-masthead-logo svg {{
  width: 100%;
  height: 100%;
  object-fit: contain;
}}
.c-logo-3d {{ color: {color["primary"]}; background: transparent; }}
.c-title-row {{
  display: flex;
  gap: {space["sm"]}px;
  align-items: center;
  margin-top: {space["md"]}px;
}}
.c-masthead .c-title-row {{ margin-top: 0; }}
.c-rank-pill {{
  min-width: 64px;
  height: 36px;
  padding: 0 12px;
  border-radius: {radius["pill"]}px;
  background: {color["primary"]};
  color: {color["text"]["onPrimary"]};
  font-family: {type_["fontFamily"]["latin"]};
  font-weight: 700;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}}
.c-hero-visual {{
  margin-top: {space["lg"]}px;
  height: 360px;
  border-radius: {radius["card"]}px;
  border: 1px solid {color["border"]};
  background:
    radial-gradient(circle at 30% 30%, {color["glow"]}, transparent 42%),
    radial-gradient(circle at 80% 70%, rgba(232, 213, 163, 0.16), transparent 36%),
    {color["surface"]};
  overflow: hidden;
  position: relative;
}}
.c-hero-visual img {{ width: 100%; height: 100%; object-fit: cover; }}
.c-hero-visual .c-function-diagram {{
  width: 100%;
  height: 100%;
  color: {color["primary"]};
  display: block;
}}
.c-story {{
  display: flex;
  flex-direction: column;
  gap: {space["md"]}px;
  padding: {space["md"]}px;
  min-height: 0;
}}
.c-story-row {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: {space["md"]}px;
  flex: 1;
  min-height: 0;
}}
.c-story-beat {{
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  border-radius: {radius["card"]}px;
  background: {color["surfaceRaised"]};
  overflow: hidden;
}}
.c-story-media {{
  flex: 1;
  min-height: 118px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: {color["primary"]};
}}
.c-story-media img {{ width: 100%; height: 100%; object-fit: cover; }}
.c-story-media .c-feature-icon {{ width: 56px; height: 56px; flex-basis: 56px; }}
.c-story-caption {{
  margin: 0;
  padding: {space["sm"]}px {space["md"]}px {space["md"]}px;
  font-size: {type_["body"]["size"]}px;
  line-height: 1.35;
  color: {color["text"]["primary"]};
}}
.c-hero-visual .c-hero-caption {{
  position: absolute;
  left: {space["lg"]}px;
  bottom: {space["lg"]}px;
  color: {color["text"]["secondary"]};
  font-size: 13px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}}
.c-quote-card {{
  margin-top: {space["lg"]}px;
  padding: {space["lg"]}px;
  border-radius: {radius["card"]}px;
  background: {color["surface"]};
  border: 1px solid {color["border"]};
}}
.c-quote-card .label {{
  font-size: 12px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: {color["primary"]};
}}
.c-quote-card p {{
  margin: {space["sm"]}px 0 0;
  font-size: {type_["subtitle"]["size"] - 4}px;
  line-height: 1.4;
  color: {color["quote"]};
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}}
.c-quote-lede {{
  margin-top: {space["sm"]}px;
  padding: 0;
  background: transparent;
  border: none;
  max-width: 78%;
}}
.c-quote-lede .label {{
  font-size: {tokens["fontSize"]["md"]}px;
  letter-spacing: 0.04em;
  text-transform: none;
  font-weight: 600;
}}
.c-quote-lede p {{
  font-size: {type_["body"]["size"] + 2}px;
  -webkit-line-clamp: 4;
}}
.c-metric-panel .c-metric-grid {{
  display: grid;
  grid-template-columns: 1fr;
  gap: {space["md"]}px;
}}
.c-metric-cell .c-metric-value {{
  text-align: left;
  font-size: {type_["h2"]["size"] - 10}px;
}}
.c-metric-cell .c-metric-label {{
  text-align: left;
}}
.c-split {{
  margin-top: {space["lg"]}px;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: {space["md"]}px;
  min-height: 0;
}}
.c-panel {{
  padding: {space["lg"]}px;
  border-radius: {radius["card"]}px;
  background: {color["surface"]};
  border: 1px solid {color["border"]};
  min-height: 0;
}}
.c-panel h3 {{
  margin: 0 0 {space["md"]}px;
  font-size: 13px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: {color["text"]["muted"]};
  font-weight: 600;
}}
.c-feature-list {{
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 10px;
}}
.c-feature-list li {{
  display: flex;
  gap: 12px;
  align-items: center;
  font-size: {type_["body"]["size"]}px;
}}
.c-feature-icon {{
  width: 32px;
  height: 32px;
  flex: 0 0 32px;
  color: {color["primary"]};
  display: block;
}}
.c-workflow {{
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}}
.c-workflow span {{
  padding: 8px 10px;
  border-radius: {radius["sm"]}px;
  background: {color["surfaceRaised"]};
  border: 1px solid {color["border"]};
  font-size: 14px;
  color: {color["text"]["secondary"]};
}}
.c-audience-card, .c-url-card {{
  margin-top: {space["md"]}px;
}}
.c-audience-card p, .c-url-card p {{
  margin: 0;
  font-size: 18px;
  line-height: 1.45;
  color: {color["text"]["secondary"]};
}}
.c-url-card p {{
  font-family: {type_["fontFamily"]["latin"]};
  color: {color["metric"]};
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}}
.template-cover,
.template-poster-a,
.template-poster-b {{
  padding: 0;
  background: {template_main};
  color: {template_ink};
}}
.c-smart-cover-shell,
.c-geometric-poster {{
  position: relative;
  width: 100%;
  height: 100%;
  overflow: hidden;
  color: {template_ink};
}}
.c-smart-cover-shell {{ padding: 64px 56px 52px; }}
.c-smart-cover-topline,
.c-smart-cover-footer {{
  position: absolute;
  left: 56px;
  right: 56px;
  display: flex;
  justify-content: space-between;
  color: {template_muted};
  font-size: 18px;
  letter-spacing: .1em;
}}
.c-smart-cover-topline {{ top: 56px; }}
.c-smart-cover-footer {{ bottom: 48px; font-size: 15px; letter-spacing: .04em; }}
.c-smart-cover-heading {{ position: absolute; left: 56px; top: 205px; max-width: 88%; }}
.c-smart-cover-heading h1 {{
  margin: 0;
  font-size: 96px;
  line-height: .98;
  letter-spacing: -.05em;
  font-weight: 700;
  max-width: 920px;
  overflow: hidden;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}}
.c-smart-cover-heading p {{
  margin: 22px 0 0;
  color: {template_muted};
  font-size: 34px;
  line-height: 1.15;
  max-width: 820px;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}}
.c-smart-cover-heading .c-smart-cover-kicker {{
  margin: 0 0 {space["sm"]}px;
  color: {template_accent};
  font-size: {type_["caption"]["size"]}px;
  font-weight: {type_["caption"]["weight"]};
  line-height: 1;
  letter-spacing: 0.24em;
  text-transform: uppercase;
  max-width: 820px;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}}
.c-smart-cover-list {{
  position: absolute;
  left: 56px;
  right: 56px;
  top: 500px;
  bottom: 170px;
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
}}
.c-smart-cover-item {{
  display: grid;
  grid-template-columns: 72px 250px minmax(0, 1fr) 110px;
  align-items: center;
  min-height: 58px;
  padding: 10px 0;
  border-bottom: 1px solid {template_border};
}}
.c-smart-cover-rank {{ color: {template_accent}; font-size: 34px; font-weight: 700; letter-spacing: -.04em; }}
.c-smart-cover-name {{ overflow: hidden; white-space: nowrap; text-overflow: ellipsis; font-size: 25px; font-weight: 600; }}
.c-smart-cover-desc {{ overflow: hidden; white-space: nowrap; text-overflow: ellipsis; color: {template_muted}; font-size: 18px; }}
.c-smart-cover-metric {{ text-align: right; color: {template_accent}; font-size: 27px; font-weight: 700; }}
.c-smart-cover-tags {{
  position: absolute;
  left: 56px;
  bottom: 98px;
  display: flex;
  gap: 18px;
  max-width: calc(100% - 112px);
  overflow: hidden;
}}
.c-smart-cover-tags span {{ color: {template_muted}; font-size: 15px; border-bottom: 1px solid {template_border}; padding-bottom: 5px; }}
.c-poster-a-shell {{ padding: 64px 56px 48px; }}
.c-poster-a-number {{ position: absolute; left: 56px; top: 54px; color: {template_accent}; font-size: 92px; line-height: 1; font-weight: 300; letter-spacing: -.04em; }}
.c-poster-a-title {{ position: absolute; left: 56px; top: 270px; width: 58%; }}
.c-poster-a-title h1,
.c-poster-b-title h1 {{
  margin: 0;
  color: {template_ink};
  font-size: 58px;
  line-height: 1.05;
  letter-spacing: -.04em;
  font-weight: 600;
  overflow: hidden;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}}
.c-poster-a-title p,
.c-poster-b-title p {{
  margin: 22px 0 0;
  color: {template_muted};
  font-size: 24px;
  line-height: 1.35;
  overflow: hidden;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
}}
.c-poster-a-lines {{
  position: absolute;
  left: 56px;
  right: 56px;
  bottom: 158px;
  margin: 0;
  padding: 0;
  list-style: none;
  border-top: 1px solid {template_border};
  max-height: 300px;
  overflow: hidden;
}}
.c-poster-a-lines li {{
  display: grid;
  grid-template-columns: 72px minmax(0, 1fr);
  gap: 16px;
  align-items: center;
  min-height: 68px;
  padding: 8px 0;
  border-bottom: 1px solid {template_border};
}}
.c-poster-a-lines li span {{ color: {template_muted}; font-size: 18px; }}
.c-poster-a-lines li b {{
  color: {template_ink};
  font-size: 23px;
  line-height: 1.2;
  font-weight: 400;
  overflow: hidden;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 1;
}}
.c-poster-a-footer,
.c-poster-b-footer {{ position: absolute; left: 56px; bottom: 48px; color: {template_muted}; font-size: 16px; letter-spacing: .08em; }}
.c-poster-b-shell {{ padding: 64px 56px 48px; }}
.c-poster-b-media {{
  position: absolute;
  left: 56px;
  top: 310px;
  width: 42%;
  height: 390px;
  border: 1px solid {template_border};
  background: {template_surface_raised};
  display: flex;
  align-items: center;
  justify-content: center;
}}
.c-poster-b-media::after {{ content: ""; position: absolute; inset: 18px; border: 1px solid {template_border}; }}
.c-poster-b-media span {{ position: relative; z-index: 1; color: {template_accent}; font-size: 72px; font-weight: 300; letter-spacing: -.06em; }}
.c-poster-b-chevron {{
  position: absolute;
  right: 56px;
  top: 55px;
  width: 310px;
  height: 62px;
  opacity: .95;
  background:
    repeating-linear-gradient(135deg, {template_muted} 0 2px, transparent 2px 34px),
    {template_secondary};
  border-top: 1px solid {template_border};
  clip-path: polygon(0 0, 76% 0, 62% 100%, 42% 100%, 56% 22%, 0 22%);
}}
.c-poster-b-title {{ position: absolute; left: 56px; top: 100px; width: 44%; }}
.c-poster-b-info {{
  position: absolute;
  right: 56px;
  top: 180px;
  width: 42%;
  height: 650px;
  margin: 0;
  padding: 0;
  list-style: none;
}}
.c-poster-b-info li {{
  display: grid;
  grid-template-columns: 48px minmax(0, 1fr);
  gap: 14px;
  min-height: 145px;
  padding: 12px 0 18px;
  border-bottom: 1px solid {template_border};
}}
.c-poster-b-info li span {{ color: {template_muted}; font-size: 17px; }}
.c-poster-b-info li p {{
  margin: 0;
  color: {template_ink};
  font-size: 22px;
  line-height: 1.35;
  overflow: hidden;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
}}
.c-poster-b-footer {{ left: auto; right: 56px; }}
""".strip()
