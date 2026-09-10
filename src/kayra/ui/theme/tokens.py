# ┌────────────────────────────────────────────────────────────────────────┐
# │                              tokens.py                                 │
# │                  Kayra Design Tokens — Single Source                   │
# └────────────────────────────────────────────────────────────────────────┘
"""
Every colour, size, radius, duration and weight in the Kayra UI is defined here and nowhere
else. No literal `#RRGGBB` and no magic pixel value belongs in a view or a component.

WHY A TOKEN LAYER
-----------------
Not tidiness. Two concrete reasons:

  * The palette below is deliberately restrained, and restraint is impossible to hold if every
    file may invent a shade. One amber becomes five ambers within a week, and the interface
    stops looking designed.
  * The whole application is themed through a generated Qt stylesheet. That generation needs a
    machine-readable palette; hand-written QSS scattered across widgets cannot be regenerated,
    audited for contrast, or changed in one place.

DESIGN DIRECTION
----------------
Technical, premium, minimal, futuristic, calm. Explicitly NOT the default AI aesthetic: no
blue-dominant surfaces, no cyan neon, no purple-blue gradients, no glowing borders.

The ground is a near-black with a faint WARM bias (a trace of red/yellow in the mix rather
than the blue-grey most dark UIs default to). It reads as graphite rather than as "dark mode",
and it is what makes a single amber accent look intentional instead of decorative.

ONE accent family: amber, with copper as its deeper partner for automation. The accent is
reserved for state, selection, focus and progress — the things the user must be able to find
instantly. Everything else is neutral. If the interface ever looks orange, the accent is being
overused.

Status colours stay semantically distinct from the accent, and are muted so a healthy system
never looks alarming: a saturated green "OK" pip is as loud as an error and trains people to
ignore both.

CONTRAST
--------
Checked against the base surface (#0E0E10):
    text.primary   #EDE9E3  ~15.8:1   (WCAG AAA)
    text.secondary #ADAAA4   ~8.2:1   (AAA for body text)
    text.tertiary  #857F78   ~4.7:1   (AA) — metadata and help text
    text.disabled  #4A4844   ~1.9:1   — genuinely unavailable controls only
    accent         #E8A33D   ~8.9:1

The gap between `tertiary` and `disabled` is deliberately wide. They were two points apart
before, so a caption and a dead control looked the same and every description on the Settings
screen read as switched off.
"""


# ┌────────────────────────────────────────────────────────────────────────┐
# │                               COLOUR                                   │
# └────────────────────────────────────────────────────────────────────────┘

class Color:
    # ── Ground and surfaces: five steps of layered graphite ──
    # Elevation is expressed by lightness alone. There are no drop shadows between panels;
    # borders and one step of lightness separate them, which stays crisp at any DPI.
    base = "#0E0E10"          # window background, the darkest thing on screen
    base_elevated = "#121214" # the content ground where it must lift off the window
    surface = "#141416"       # panels, sidebar
    elevated = "#1A1B1E"      # cards sitting on a panel
    overlay = "#212126"       # menus, popovers
    inset = "#0A0A0C"         # wells: inputs, code, chat scroll area

    # ── Interaction states, named rather than improvised per widget ──
    # Every hover in the application is ONE step up the ladder and every press is one step
    # down. Naming them is what stops each component inventing its own idea of "slightly
    # lighter", which is how a dark UI ends up with six different hover greys.
    surface_hover = "#212126"
    surface_active = "#282830"

    # ── Glass: the floating layer ──
    # A FIFTH surface concept, and a different KIND from the four above. Those are opaque
    # steps of a lightness ladder and they describe things that sit IN the page. These
    # describe things that float OVER it — the dock, the navigation drawer, the window
    # chrome — and they are semi-transparent so the ambient backdrop shows through.
    #
    # WHY NOT JUST A LIGHTER OPAQUE GREY. Qt has no backdrop-filter, so a real frosted blur
    # is not available in a stylesheet; what sells "glass" here is the transparency itself
    # plus a hairline top edge that reads as a lit rim. An opaque panel over a moving
    # backdrop reads as a hole punched in the page, which is the opposite of floating.
    #
    # The alpha values are deliberately high (0.72-0.88). Anything more transparent stops
    # being a surface: text on it competes with whatever is behind, and the whole point of
    # the dock is that its controls stay instantly readable over any content.
    glass = "rgba(26, 27, 30, 0.82)"
    glass_strong = "rgba(20, 20, 22, 0.92)"
    glass_hover = "rgba(40, 40, 48, 0.90)"
    glass_active = "rgba(52, 52, 62, 0.94)"
    glass_border = "rgba(255, 255, 255, 0.08)"
    glass_border_strong = "rgba(255, 255, 255, 0.14)"
    # The lit top edge. One hairline, brighter than the border, is what makes a translucent
    # panel read as a physical object catching light rather than as a tinted rectangle.
    glass_rim = "rgba(255, 255, 255, 0.10)"

    # ── The ambient backdrop ──
    # Painted behind everything by `components/backdrop.py`. Very low contrast on purpose:
    # the requirement is that text stays highly readable, so these sit within a few points
    # of the base ground and are visible as depth rather than as pattern.
    backdrop_top = "#101013"      # the faint vertical wash, top
    backdrop_bottom = "#0B0B0D"   # ... and bottom
    backdrop_grid = "#17171C"     # the geometric hairlines
    backdrop_bloom = "#2A1F10"    # the warm radial bloom behind the orb
    backdrop_particle = "#3A3630"  # drifting motes

    # ── Lines ──
    border = "#2A2A30"        # default separator: visible, never assertive
    border_strong = "#3A3A43" # focused inputs, active card edges
    border_subtle = "#202024" # inside dense groups where a full border is too much

    # ── Type ──
    text = "#EDE9E3"          # warm white — not pure #FFF, which glares on near-black
    text_secondary = "#ADAAA4"
    # Lifted from #6E6B66 (3.3:1). At that value captions, help text and metadata sat close
    # enough to `text_disabled` that a live description read as an unavailable one — and this
    # tier carries the settings help, the timestamps and the metric captions, which is a lot
    # of the interface to render as "greyed out". #857F78 measures ~4.7:1 on the base ground.
    text_tertiary = "#857F78"
    text_disabled = "#4A4844"
    text_on_accent = "#17130B" # dark ink on amber; white on amber fails contrast

    # ── Accent: amber, used sparingly ──
    accent = "#E8A33D"
    accent_hover = "#F2B457"
    accent_press = "#CE8C2C"
    accent_muted = "#7A5A25"   # de-emphasised accent (inactive tab underline)
    accent_subtle = "#4A3618"  # accent at reading weight for borders that must not shout
    accent_wash = "#1F1810"    # accent-tinted fill for selected rows
    accent_glow = "#3A2A12"    # soft halo behind the assistant visual

    # ── Copper: the accent's deeper partner, reserved for automation ──
    # A second hue in the same warm family gives automation its own identity without
    # introducing a competing colour temperature.
    copper = "#C2703C"
    copper_wash = "#1E1410"
    # `automation` is the semantic name; `copper` is the pigment. Views ask for meaning.
    automation = copper
    automation_wash = copper_wash

    # Disabled is a surface + a text colour, not an opacity: fading a widget on a near-black
    # ground makes it disappear rather than read as unavailable.
    disabled = "#1A1A1D"

    # ── Status: muted, but not invisible ──
    # The washes were originally near-black (#121A13 is 3 points off the card it sits on), so
    # a "Done" chip and a "Blocked" chip were the same dark rectangle with slightly different
    # text — the distinction the user most needs to make at a glance was the one hardest to
    # see. Lifted to roughly one surface step above the card, which is enough to read as a
    # tinted field without becoming a bright web badge.
    success = "#7ACD7E"
    success_wash = "#16241A"
    success_edge = "#2E5535"
    warning = "#E0AC4B"
    warning_wash = "#26200F"
    warning_edge = "#5B4820"
    danger = "#E3796A"
    danger_wash = "#2A1715"
    danger_edge = "#5E322C"
    neutral = "#8C8A85"

    # ── Assistant states ──
    # The orb is the one place colour carries primary meaning, so each state is distinct at a
    # glance while staying inside the warm palette.
    state_idle = "#5A5750"
    state_listening = "#E8A33D"
    state_thinking = "#C2703C"
    state_speaking = "#F2B457"
    state_automating = "#D98A3D"
    state_proactive = "#6FBF73"
    state_error = "#D96A5A"

    # ── Data visualisation: sequential warm ramp for meters ──
    meter_low = "#6FBF73"
    meter_mid = "#D9A441"
    meter_high = "#D96A5A"
    meter_track = "#202024"


# ┌────────────────────────────────────────────────────────────────────────┐
# │                             TYPOGRAPHY                                 │
# └────────────────────────────────────────────────────────────────────────┘

class Font:
    """
    Two families, both already present on Windows, so the UI never waits on a font download
    and never falls back to something unintended.

      ui   — Segoe UI Variable Text is the Windows 11 system face; matching it is what makes
             the application feel native rather than transplanted.
      mono — Cascadia Mono for anything numeric or technical. Tabular figures stop metrics
             from jittering as digits change, which is the whole reason a monospace face
             belongs in a dashboard.
    """
    ui = '"Segoe UI Variable Text", "Segoe UI", "Inter", system-ui, sans-serif'
    ui_display = '"Segoe UI Variable Display", "Segoe UI", "Inter", system-ui, sans-serif'
    mono = '"Cascadia Mono", "Consolas", "SF Mono", monospace'

    # A restrained scale. Seven sizes is enough for any screen here; more invites inconsistency.
    micro = 10      # dense metric captions
    caption = 11    # metadata, timestamps
    small = 12      # secondary body, table cells
    body = 13       # default
    subtitle = 15   # card titles
    title = 20      # page titles
    display = 30    # the single hero line on Home
    metric = 22     # a large numeric readout (a score, a percentage)
    metric_lg = 34  # the one headline number on a page

    light = 300
    regular = 400
    medium = 500
    semibold = 600

    # Letter-spacing for the small uppercase labels that give the UI its technical register.
    # Uppercase text at 10-11px is unreadable without it.
    tracking_label = 1.2


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        SPACING / GEOMETRY                              │
# └────────────────────────────────────────────────────────────────────────┘

class Space:
    """A 4px base grid. Every margin, padding and gap is one of these."""
    xxs = 2
    xs = 4
    sm = 8
    md = 12
    base = 16
    lg = 24
    xl = 32
    xxl = 48
    section = 40


class Radius:
    """Subtle, not pill-shaped. Large radii on dark panels read as toy-like."""
    sm = 4
    md = 6
    lg = 10
    xl = 14
    pill = 999


class Size:
    sidebar = 224
    sidebar_collapsed = 60
    titlebar = 44
    nav_item = 38
    control = 32
    control_lg = 40
    input = 38
    orb_home = 208
    orb_compact = 34
    ambient_width = 320
    ambient_height = 96
    # The readable measure. Body text past roughly this width stops scanning cleanly, and a
    # 2560px monitor should not produce 2000px-long lines. Pages centre their column in it.
    content_max = 1180
    # Chat's own measure, narrower than a document page. A transcript alternates left- and
    # right-aligned bubbles, so the eye travels the FULL width on every turn rather than
    # returning to a fixed left margin — which makes a wide column tiring long before a
    # document one would be.
    chat_max = 900
    # Every settings control is this wide. Ragged right edges down a settings page are the
    # single most obvious sign that a form was assembled rather than designed.
    control_field = 300
    settings_label_max = 460
    # ── The floating dock (Home and Chat only) ──
    dock_height = 56
    dock_radius = 28              # exactly half the height: a true pill
    dock_button = 40
    dock_margin_bottom = 28       # clearance from the window edge
    # ── The navigation drawer (Home and Chat only) ──
    drawer_width = 268
    # ── Custom window chrome ──
    chrome_height = 40
    chrome_button = 46            # Windows caption buttons are wider than they are tall
    resize_margin = 6             # the grab band Windows hit-tests for resizing

    min_window_width = 1040
    min_window_height = 680
    default_window_width = 1280
    default_window_height = 820
    scrollbar = 10


class Motion:
    """
    Durations in milliseconds.

    Deliberately short. Motion here exists to explain a change of state, not to perform; above
    roughly 250ms a UI transition stops feeling responsive and starts feeling slow. The orb is
    the one continuously animated element, and it runs at a capped frame rate that drops to
    zero when the window is hidden.
    """
    instant = 90
    fast = 140
    normal = 200
    slow = 320
    orb_fps = 30            # capped; the orb is idle-cheap and stops entirely when hidden
    orb_fps_idle = 12       # slower still when the assistant is doing nothing
    metrics_interval = 2000
    activity_interval = 1500

    # ── The shell's motion ──
    # SLOWER THAN THE REST OF THE UI, DELIBERATELY. The durations above govern a control
    # responding to a press, where anything over ~250ms reads as lag. These govern a SURFACE
    # arriving or leaving, which is a different event: a panel that snaps into place at 140ms
    # reads as a jump cut, and the whole complaint this pass answers was that navigation felt
    # abrupt. Around a third of a second is where a movement stops being noticed as a delay
    # and starts being read as the thing moving.
    drawer = 360              # panel slide + scrim fade (300–450ms target)
    drawer_items = 360        # synchronized with panel slide
    drawer_stagger = 0        # unified drawer motion without delayed pop-in
    nav_transition = 360      # a page arriving: smooth dual-surface crossfade (250–400ms target)
    nav_travel = 0            # px. 0 for a seamless, calm crossfade without lateral twitching
    indicator = 280           # the active-item rail moving between destinations
    dock_hover = 180          # dock interaction hover transition
    press = 100
    # The ambient backdrop. SLOW is the whole point: at 8fps and a 90-second cycle the
    # motion is felt rather than watched, which is what keeps it from competing with the
    # interface. It also means the backdrop costs less per second than the orb does.
    backdrop_fps = 8
    backdrop_cycle_s = 90.0


class Elevation:
    """Shadows are used for genuinely floating things only: menus, the ambient window."""
    none = "none"
    popover = "0 8px 24px rgba(0, 0, 0, 0.55)"
    floating = "0 12px 40px rgba(0, 0, 0, 0.65)"
    # The dock sits above content and must read as lifted off it, not stuck to it. Drawn with
    # a QGraphicsDropShadowEffect rather than QSS (Qt does not honour `box-shadow`), so this
    # string is documentation of the intent; `dock_shadow_*` are the values actually used.
    dock = "0 18px 48px rgba(0, 0, 0, 0.70)"
    dock_shadow_blur = 44
    dock_shadow_y = 14
    dock_shadow_alpha = 170
    drawer_shadow_blur = 56
    drawer_shadow_x = 18
    drawer_shadow_alpha = 190


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        SEMANTIC MAPPINGS                               │
# └────────────────────────────────────────────────────────────────────────┘
# Backend vocabulary translated into presentation. Views ask for meaning, never for a hex code,
# which is what keeps the palette changeable in one place.

# Maps `kayra.core.runtime_state.AssistantState` plus UI-only states.
STATE_COLORS = {
    "IDLE": Color.state_idle,
    "LISTENING": Color.state_listening,
    "PROCESSING": Color.state_thinking,
    "SPEAKING": Color.state_speaking,
    "INTERRUPTING": Color.state_error,
    "AUTOMATING": Color.state_automating,
    "SHUTTING_DOWN": Color.state_idle,
    "PROACTIVE": Color.state_proactive,
    "ERROR": Color.state_error,
    "STARTING": Color.state_thinking,
    "OFFLINE": Color.state_idle,
}

STATE_LABELS = {
    "IDLE": "Idle",
    "LISTENING": "Listening",
    "PROCESSING": "Thinking",
    "SPEAKING": "Speaking",
    "INTERRUPTING": "Interrupted",
    "AUTOMATING": "Working",
    "SHUTTING_DOWN": "Shutting down",
    "PROACTIVE": "Suggesting",
    "ERROR": "Error",
    "STARTING": "Starting",
    "OFFLINE": "Offline",
}

# Maps `kayra.core.system_profile` verdicts.
VERDICT_COLORS = {
    "READY": Color.success,
    "GOOD": Color.success,
    "LIMITED": Color.warning,
    "REQUIRES_CONFIGURATION": Color.warning,
    "NOT_AVAILABLE": Color.danger,
}

VERDICT_LABELS = {
    "READY": "Ready",
    "GOOD": "Good",
    "LIMITED": "Limited",
    "REQUIRES_CONFIGURATION": "Needs setup",
    "NOT_AVAILABLE": "Unavailable",
}


def meter_color(percent):
    """
    Colour for a utilisation meter.

    Thresholds are generous on purpose: a machine at 60% is working, not struggling, and a
    dashboard that turns amber at every mild load teaches the user to ignore it.
    """
    if percent >= 90:
        return Color.meter_high
    if percent >= 70:
        return Color.meter_mid
    return Color.meter_low


def with_alpha(hex_color, alpha):
    """`#RRGGBB` + 0..1 -> `rgba(r, g, b, a)` for stylesheet use."""
    value = hex_color.lstrip("#")
    r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r}, {g}, {b}, {alpha:.3f})"
