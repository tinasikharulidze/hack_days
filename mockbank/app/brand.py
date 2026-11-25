"""Look and feel of the pages the mock bank shows to a customer.

Only the approval pages (step 3) are visible to a person, so this is the
one place that decides how they look. The colours come from the project's
brand picture: bubblegum pink, galactic violet, a soft sky blue and
marshmallow white.

page() wraps any piece of HTML in the shared layout.
"""

import html

# --- The palette ---
PINK = "#FD6AD2"
VIOLET = "#553EFE"
BLUE = "#7CC4F5"
WHITE = "#F0F0F0"
INK = "#1B1740"      # dark violet used for text

CSS = f"""
* {{ box-sizing: border-box; }}
body {{
  margin: 0; min-height: 100vh; display: flex; flex-direction: column; align-items: center;
  justify-content: center; padding: 24px; color: {INK};
  font-family: "Segoe UI", system-ui, -apple-system, Roboto, Helvetica, Arial, sans-serif;
  /* the soft pink-to-violet glow of the brand cover, on marshmallow white */
  background: radial-gradient(circle at 12% 95%, {PINK}66 0, transparent 45%),
              radial-gradient(circle at 92% 90%, {VIOLET}55 0, transparent 50%),
              radial-gradient(circle at 80% 5%, {BLUE}55 0, transparent 40%), {WHITE};
}}
.card {{
  width: 100%; max-width: 460px; background: #fff; border-radius: 22px; padding: 28px;
  box-shadow: 0 18px 50px {VIOLET}26;
}}
.brand {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 18px; }}
.logo {{ font-weight: 800; font-size: 22px; letter-spacing: -0.5px; color: {VIOLET}; }}
.logo span {{ color: {PINK}; }}
.badge {{
  font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .6px;
  background: {PINK}26; color: #B0238A; padding: 4px 10px; border-radius: 999px;
}}
h1 {{ font-size: 22px; margin: 0 0 6px; letter-spacing: -0.3px; }}
p {{ margin: 0 0 14px; line-height: 1.45; }}
.muted {{ color: #6b6890; font-size: 14px; }}
.info {{
  background: {BLUE}2e; border-radius: 14px; padding: 12px 14px; font-size: 14px; margin: 14px 0;
}}
.info b {{ color: {VIOLET}; }}
.error {{
  background: #ff4d6d1a; color: #b3123a; border-radius: 12px; padding: 10px 14px;
  font-size: 14px; margin: 12px 0;
}}
label {{ display: block; font-size: 13px; font-weight: 600; margin: 12px 0 5px; }}
input[type=text], input[type=password] {{
  width: 100%; padding: 12px 14px; font-size: 15px; border-radius: 12px;
  border: 1.5px solid #d9d6ee; outline: none; background: {WHITE};
}}
input[type=text]:focus, input[type=password]:focus {{ border-color: {VIOLET}; background: #fff; }}
.account {{
  display: flex; gap: 12px; align-items: flex-start; padding: 12px 14px; margin: 8px 0;
  border: 1.5px solid #e4e1f5; border-radius: 14px; cursor: pointer; font-weight: 400;
}}
.account input {{ margin-top: 4px; accent-color: {VIOLET}; width: 17px; height: 17px; }}
.account b {{ display: block; font-size: 15px; }}
.account small {{ color: #6b6890; font-size: 12.5px; word-break: break-all; }}
.buttons {{ display: flex; gap: 10px; margin-top: 20px; }}
button {{
  flex: 1; padding: 13px; font-size: 15px; font-weight: 700; border: 0; border-radius: 999px;
  cursor: pointer; transition: transform .08s, box-shadow .15s;
}}
button:hover {{ transform: translateY(-1px); }}
.primary {{ background: linear-gradient(90deg, {VIOLET}, #8A4DFF); color: #fff;
            box-shadow: 0 8px 20px {VIOLET}44; }}
.secondary {{ background: {PINK}26; color: #B0238A; }}
.foot {{ margin-top: 16px; font-size: 12px; color: #6b6890; text-align: center; }}
"""


def esc(value) -> str:
    """Make text safe to put inside HTML."""
    return html.escape(str(value), quote=True)


def page(title: str, body: str) -> str:
    """Wrap a piece of HTML in the shared layout: logo, test badge, card, footer."""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · MockBank</title><style>{CSS}</style></head>
<body><div class="card">
  <div class="brand"><div class="logo">Mock<span>Bank</span></div><div class="badge">Test bank</div></div>
  {body}
  <div class="foot">This is a test bank. Nothing here is real and no money moves.</div>
</div></body></html>"""
