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
    # Cover/poster chrome uses a compact three-color vocabulary. Keep
    # those layers derived from resolved tokens so the picker changes pixels.
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
.template-cover,
.template-poster-a,
.template-poster-b {{
  padding: 0;
  background: {template_main};
  color: {template_ink};
}}
.c-smart-cover-shell {{
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
.skin-toc {{
  padding: 0;
  font-family: Georgia, "Times New Roman", "Songti SC", serif;
  color: {template_ink};
  background: {template_main};
}}
.skin-toc::before,
.skin-toc::after,
.skin-toc.template-cover::before,
.skin-toc.template-cover::after,
.skin-toc.template-poster-a::before,
.skin-toc.template-poster-a::after,
.skin-toc.template-poster-b::before,
.skin-toc.template-poster-b::after {{
  display: none;
}}
.skin-toc.template-cover,
.skin-toc.template-poster-a,
.skin-toc.template-poster-b {{
  border: 0;
  background: {template_main};
}}
.toc-stage {{
  position: relative;
  width: {canvas["width"]}px;
  height: {canvas["height"]}px;
  overflow: hidden;
  background:
    radial-gradient(circle at 14% 22%, rgba(255,255,255,.22) 0 2px, transparent 2.1px),
    radial-gradient(circle at 75% 28%, rgba(255,255,255,.16) 0 1.6px, transparent 1.7px),
    radial-gradient(circle at 31% 68%, rgba(122,90,46,.08) 0 1.1px, transparent 1.2px),
    radial-gradient(circle at 82% 74%, rgba(122,90,46,.06) 0 1px, transparent 1.1px),
    linear-gradient(180deg, rgba(255,255,255,.08), rgba(0,0,0,.025)),
    repeating-radial-gradient(circle at 50% 50%, rgba(255,255,255,.045) 0 2px, rgba(0,0,0,.02) 2px 4px),
    {template_main};
}}
.toc-frame {{ position: absolute; pointer-events: none; z-index: 3; }}
.toc-frame.h {{
  height: 1px;
  background: {template_accent};
  box-shadow: 0 1px 0 rgba(255,255,255,.26), 0 0 0 1px rgba(159,125,79,.08);
}}
.toc-frame.v {{
  width: 1px;
  background: {template_accent};
  box-shadow: 1px 0 0 rgba(255,255,255,.24), 0 0 0 1px rgba(159,125,79,.08);
}}
.toc-frame.soft.h {{ background: {template_border}; box-shadow: 0 1px 0 rgba(255,255,255,.2); }}
.toc-frame.soft.v {{ background: {template_border}; box-shadow: 1px 0 0 rgba(255,255,255,.2); }}
.toc-stage .top-a {{ left: 17px; top: 17px; width: {canvas["width"] - 34}px; }}
.toc-stage .top-b {{ left: 25px; top: 24px; width: {canvas["width"] - 50}px; }}
.toc-stage .bottom-a {{ left: 17px; bottom: 17px; width: {canvas["width"] - 34}px; }}
.toc-stage .bottom-b {{ left: 25px; bottom: 24px; width: {canvas["width"] - 50}px; }}
.toc-stage .left-a {{ left: 17px; top: 17px; height: {canvas["height"] - 34}px; }}
.toc-stage .left-b {{ left: 25px; top: 25px; height: {canvas["height"] - 50}px; }}
.toc-stage .right-a {{ right: 17px; top: 17px; height: {canvas["height"] - 34}px; }}
.toc-stage .right-b {{ right: 25px; top: 25px; height: {canvas["height"] - 50}px; }}
.toc-inner-line {{
  position: absolute;
  left: 47px; top: 47px;
  width: {canvas["width"] - 94}px; height: {canvas["height"] - 94}px;
  border: 1px solid rgba(177,149,110,.72);
  box-shadow: inset 0 0 0 1px rgba(255,255,255,.05);
  pointer-events: none;
  z-index: 3;
}}
.toc-corner {{
  position: absolute;
  width: 23px; height: 23px;
  background: transparent;
  border: 1px solid {template_accent};
  box-shadow: 0 0 0 1px rgba(255,255,255,.14) inset, 0 0 0 1px rgba(159,125,79,.08);
  z-index: 4;
  pointer-events: none;
}}
.toc-corner::before {{
  content: "";
  position: absolute; inset: 1px;
  border: 1px solid rgba(211,188,149,.42);
}}
.toc-corner.tl {{ left: 6px; top: 7px; }}
.toc-corner.tr {{ right: 6px; top: 7px; }}
.toc-corner.bl {{ left: 6px; bottom: 7px; }}
.toc-corner.br {{ right: 6px; bottom: 7px; }}
.toc-content {{
  position: absolute;
  left: 72px; right: 72px; top: 86px; bottom: 72px;
  display: flex;
  flex-direction: column;
  align-items: center;
  z-index: 2;
}}
.toc-title {{
  margin: 0 0 36px;
  font-size: 42px;
  line-height: 1.05;
  font-weight: 600;
  text-align: center;
  color: {template_ink};
}}
.toc-grid {{
  width: 100%;
  flex: 1 1 auto;
  display: grid;
  grid-template-columns: 1fr 1fr;
  grid-auto-rows: minmax(0, 1fr);
  column-gap: 28px;
  row-gap: 18px;
  align-content: start;
}}
.toc-item {{ text-align: center; min-width: 0; }}
.toc-rule {{
  width: 38px; height: 2px;
  background: linear-gradient(180deg, {template_accent}, {template_border});
  margin: 0 auto 8px auto;
}}
.toc-num {{
  margin: 0;
  font-size: 22px;
  line-height: 1;
  font-weight: 700;
  color: {template_ink};
}}
.toc-skill {{
  margin: 8px auto 7px auto;
  width: 92%;
  min-height: 38px;
  font-size: 22px;
  line-height: 1.15;
  font-weight: 700;
  color: {template_ink};
}}
.toc-desc {{
  margin: 0 auto;
  width: 90%;
  font-size: 15px;
  line-height: 1.33;
  font-weight: 400;
  color: {template_muted};
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
  overflow: hidden;
}}
.skin-toc .c-brand-header,
.skin-toc .c-project-shell,
.skin-toc .c-footer {{
  position: relative;
  z-index: 2;
}}
.skin-toc.page-project .toc-stage {{
  padding: 72px 64px 60px;
  display: flex;
  flex-direction: column;
}}
.skin-toc.template-poster-a.page-project,
.skin-toc.template-poster-b.page-project {{
  padding: 0;
}}
.skin-toc .c-panel,
.skin-toc.template-poster-a .c-panel,
.skin-toc.template-poster-b .c-panel {{
  background: rgba(255,255,255,.16);
  border-color: rgba(177,149,110,.72);
  border-radius: 0;
  box-shadow: none;
}}
.skin-toc .c-quote-card {{
  background: rgba(255,255,255,.12);
  border-color: rgba(177,149,110,.72);
}}
""".strip()
