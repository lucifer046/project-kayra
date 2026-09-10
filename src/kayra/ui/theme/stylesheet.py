# ┌────────────────────────────────────────────────────────────────────────┐
# │                           stylesheet.py                                │
# │             The Application Stylesheet, Built From Tokens              │
# └────────────────────────────────────────────────────────────────────────┘
"""
Generates the single Qt stylesheet applied once to the QApplication.

WHY ONE GENERATED SHEET
-----------------------
Qt lets any widget carry its own stylesheet, and that freedom is a trap: per-widget sheets are
re-parsed on every polish, they cascade to children in ways that are hard to predict, and they
scatter the palette across dozens of files so it can never be changed in one place.

One sheet applied to the application object is parsed once, and widgets opt into a look by
setting `objectName` or a dynamic property. Views therefore contain layout code and nothing
else, which is the point of the split.

DYNAMIC PROPERTIES
------------------
Variant styling uses Qt property selectors, e.g. `QPushButton[variant="accent"]`. Changing a
property at runtime needs an explicit repolish — `theme.repolish(widget)` does that — because
Qt does not re-evaluate selectors on property change by itself. That is a real footgun and the
helper exists so no call site has to remember it.
"""

from kayra.ui.theme.tokens import Color, Font, Space, Radius, Size, with_alpha


def build() -> str:
    """The complete application stylesheet."""
    c, f, s, r, z = Color, Font, Space, Radius, Size

    return f"""
/* ─────────────────────────── BASE ─────────────────────────── */
* {{
    outline: none;
}}

QWidget {{
    background-color: transparent;
    color: {c.text};
    font-family: {f.ui};
    font-size: {f.body}px;
    font-weight: {f.regular};
}}

QMainWindow, QDialog {{
    background-color: {c.base};
}}

/* The root surface is TRANSPARENT, not filled. `AmbientBackdrop` is the first child and
   paints the ground; a filled root would sit on top of it and the backdrop would never be
   seen. Everything layered above still declares its own background. */
#RootSurface {{
    background-color: transparent;
}}

QToolTip {{
    background-color: {c.overlay};
    color: {c.text};
    border: 1px solid {c.border_strong};
    border-radius: {r.sm}px;
    padding: {s.xs}px {s.sm}px;
    font-size: {f.small}px;
}}

/* ───────────────────────── TYPOGRAPHY ─────────────────────── */
#PageTitle {{
    font-family: {f.ui_display};
    font-size: {f.title}px;
    font-weight: {f.semibold};
    color: {c.text};
    /* Display faces track loose at large sizes; pulling it back in makes the title read as
       one word-shape rather than a row of letters. */
    letter-spacing: -0.2px;
}}

#PageSubtitle {{
    font-size: {f.body}px;
    color: {c.text_secondary};
}}

#SectionLabel {{
    font-size: {f.caption}px;
    font-weight: {f.semibold};
    color: {c.text_secondary};
    letter-spacing: {f.tracking_label}px;
}}

#CardTitle {{
    font-size: {f.subtitle}px;
    font-weight: {f.semibold};
    color: {c.text};
}}

#Caption {{
    font-size: {f.caption}px;
    color: {c.text_tertiary};
}}

/* The assistant's own state, in the sidebar footer. One weight up from a caption: it is the
   answer to "is it working?", which is the question the footer exists for. */
#StatusName {{
    font-size: {f.small}px;
    font-weight: {f.medium};
    color: {c.text};
}}

#ListeningStatus[paused="true"] {{
    color: {c.danger};
    font-weight: {f.medium};
}}

#Secondary {{
    color: {c.text_secondary};
}}

/* What Kayra did, in a list of actions. One step up from #Secondary in both weight and
   colour, because the action is the content of the row and the badge beside it is the
   annotation — the previous styling had them the same weight and the eye had nowhere to
   land. */
#ActionName, #SubsystemName {{
    font-size: {f.body}px;
    font-weight: {f.medium};
    color: {c.text};
}}

#Mono, #MetricValue {{
    font-family: {f.mono};
    font-size: {f.small}px;
    font-weight: {f.medium};
    color: {c.text};
}}

#MetricValue {{
    font-size: {f.subtitle}px;
    font-weight: {f.medium};
}}

/* ── The Home wordmark ──
   The identity line that occupies the empty upper half of Home's centre column. It is
   SMALLER and QUIETER than #HeroLine on purpose: the sidebar already says who this is, so
   this one earns its place by composing the column and leading the eye down to the orb,
   not by announcing the product a second time. */
#HomeWordmark {{
    font-family: {f.ui_display};
    font-size: {f.wordmark}px;
    font-weight: {f.semibold};
    letter-spacing: {f.tracking_wordmark}px;
    color: {c.text};
}}

#HomeTagline {{
    font-family: {f.ui};
    font-size: {f.caption}px;
    font-weight: {f.regular};
    letter-spacing: {f.tracking_tagline}px;
    color: {c.text_tertiary};
}}

#HeroLine {{
    font-family: {f.ui_display};
    font-size: {f.display}px;
    font-weight: {f.light};
    color: {c.text};
}}

/* Large numeric readouts. Tabular figures via the mono face, so a value that changes from
   99 to 100 does not shift everything beside it. */
#Metric {{
    font-family: {f.mono};
    font-size: {f.metric}px;
    font-weight: {f.medium};
    color: {c.text};
}}

#MetricLarge {{
    font-family: {f.mono};
    font-size: {f.metric_lg}px;
    font-weight: {f.medium};
    color: {c.text};
}}

#MetricUnit {{
    font-family: {f.ui};
    font-size: {f.caption}px;
    color: {c.text_tertiary};
}}

/* A card title's quieter sibling, for groups inside a card. */
#GroupLabel {{
    font-size: {f.small}px;
    font-weight: {f.semibold};
    color: {c.text_secondary};
}}

/* ─────────────────────────── SHELL ────────────────────────── */
/* The rail is now TRANSLUCENT rather than a solid panel, so the ambient backdrop reads
   continuously behind it and the whole window looks like one surface with a column drawn on
   it — instead of two rectangles butted together, which is what made the old rail read as a
   generic admin sidebar. The right edge is a hairline, not a border colour step. */
#Sidebar {{
    background-color: {c.glass};
    border-right: 1px solid {c.glass_border};
}}

/* ─────────────────── WINDOW CHROME (frameless) ────────────── */
#WindowChrome {{
    background-color: {c.glass_strong};
    border-bottom: 1px solid {c.glass_border};
}}

#ChromeBrand {{
    font-family: {f.ui_display};
    font-size: {f.caption}px;
    font-weight: {f.semibold};
    color: {c.text};
    letter-spacing: 2.2px;
}}

/* The screen's name, deliberately quieter than the brand: a title bar should say which
   application you are in first and which screen second. */
#ChromeTitle {{
    font-size: {f.caption}px;
    font-weight: {f.regular};
    color: {c.text_tertiary};
    letter-spacing: 0.3px;
}}

/* ───────────────────── THE FLOATING DOCK ──────────────────── */
/* Semi-transparent so the page shows through, a hairline border for the edge, and a radius
   of exactly half the height so it is a true pill rather than a rounded rectangle. Qt does
   not clamp an oversized border-radius the way CSS does — `999px` here would fall back to a
   small radius and the dock would render as a box, which is the same trap StatusPill hit. */
#FloatingDock {{
    background-color: {c.glass};
    border: 1px solid {c.glass_border_strong};
    border-top: 1px solid {c.glass_rim};
    border-radius: {z.dock_radius}px;
}}

/* ─────────────────── THE NAVIGATION DRAWER ────────────────── */
#DrawerPanel {{
    background-color: {c.glass_strong};
    border-right: 1px solid {c.glass_border_strong};
}}

/* ───────────────────────── GLASS PANEL ────────────────────── */
/* The dashboard surface on Home. A Card is opaque and sits IN the page; a GlassPanel is
   translucent and floats OVER the backdrop, which is what lets the bloom behind the orb bleed
   through the panels around it instead of stopping dead at their edges. */
#GlassPanel {{
    background-color: {c.glass};
    border: 1px solid {c.glass_border};
    border-top: 1px solid {c.glass_rim};
    border-radius: {r.xl}px;
}}

#GlassPanelTitle {{
    font-size: {f.caption}px;
    font-weight: {f.semibold};
    color: {c.text_tertiary};
    letter-spacing: {f.tracking_label}px;
    text-transform: uppercase;
}}

#TitleBar {{
    background-color: {c.surface};
    border-bottom: 1px solid {c.border_subtle};
}}

#BrandMark {{
    font-family: {f.ui_display};
    font-size: {f.subtitle}px;
    font-weight: {f.semibold};
    color: {c.text};
    letter-spacing: 2.5px;
}}

/* TRANSPARENT, so the ambient backdrop is visible through every screen.
   This was `background-color: base` and it was an opaque sheet covering the whole content
   area — the backdrop painted correctly underneath it and not one pixel of it reached the
   screen. Anything that needs a ground of its own (a card, a panel, an input well) declares
   one; the page itself must not. */
#ContentArea {{
    background-color: transparent;
}}

/* ───────────────────────── NAVIGATION ─────────────────────── */
/* The active item is marked by an amber left rule plus a tinted fill. A fill alone is too
   quiet on this ground; a coloured label alone fails for anyone who cannot separate the hues. */
QPushButton#NavItem {{
    background-color: transparent;
    border: none;
    border-left: 2px solid transparent;
    /* ROUNDED, and inset from the rail's edge. The old full-bleed row with a hard left rule
       is the shape every admin template ships; a rounded plate that floats inside a padded
       column is what makes the rail match the dock and the drawer. */
    border-radius: {r.lg}px;
    padding: 0px {s.md}px 0px {s.md}px;
    text-align: left;
    color: {c.text_secondary};
    font-size: {f.body}px;
    min-height: {z.nav_item}px;
}}

QPushButton#NavItem:hover {{
    background-color: {c.surface_hover};
    color: {c.text};
}}

/* NO LEFT BORDER HERE ANY MORE. The active mark is `NavIndicator`, a real widget that
   SLIDES between destinations — a property selector is applied instantly and there is
   nothing in it to animate, which is what made changing screens feel like a jump cut. What
   the stylesheet still owns is the fill and the weight, which are properties of the row
   rather than of the mark. */
QPushButton#NavItem:checked {{
    background-color: {c.accent_wash};
    border-left: 2px solid transparent;
    color: {c.text};
    font-weight: {f.medium};
}}

QPushButton#NavItem:focus {{
    background-color: {c.elevated};
    border-left: 2px solid {c.accent_muted};
}}

/* ─────────────────────────── CARDS ────────────────────────── */
#Card {{
    background-color: {c.elevated};
    border: 1px solid {c.border_subtle};
    border-radius: {r.lg}px;
}}

#Card[interactive="true"]:hover {{
    border: 1px solid {c.border_strong};
}}

#CardFlat {{
    background-color: {c.surface};
    border: 1px solid {c.border_subtle};
    border-radius: {r.md}px;
}}

#Well {{
    background-color: {c.inset};
    border: 1px solid {c.border_subtle};
    border-radius: {r.md}px;
}}

#Divider {{
    background-color: {c.border_subtle};
    max-height: 1px;
    min-height: 1px;
    border: none;
}}

#VDivider {{
    background-color: {c.border_subtle};
    max-width: 1px;
    min-width: 1px;
    border: none;
}}

/* ────────────────────────── BUTTONS ───────────────────────── */
QPushButton {{
    background-color: {c.overlay};
    color: {c.text};
    border: 1px solid {c.border};
    border-radius: {r.md}px;
    padding: {s.sm}px {s.base}px;
    font-size: {f.small}px;
    font-weight: {f.medium};
    min-height: {z.control}px;
}}

QPushButton:hover {{
    background-color: {c.surface_hover};
    border-color: {c.border_strong};
}}

QPushButton:pressed {{
    background-color: {c.surface_active};
}}

QPushButton:disabled {{
    color: {c.text_disabled};
    background-color: {c.surface};
    border-color: {c.border_subtle};
}}

QPushButton:focus {{
    border-color: {c.accent_muted};
}}

QPushButton[variant="accent"] {{
    background-color: {c.accent};
    color: {c.text_on_accent};
    border: 1px solid {c.accent};
    font-weight: {f.semibold};
}}

QPushButton[variant="accent"]:hover {{
    background-color: {c.accent_hover};
    border-color: {c.accent_hover};
}}

QPushButton[variant="accent"]:pressed {{
    background-color: {c.accent_press};
}}

/* A disabled accent button is the loudest dead control a screen can have. The generic
   :disabled rule loses to the more specific [variant] selector, so it needs its own. */
QPushButton[variant="accent"]:disabled {{
    background-color: {c.disabled};
    border-color: {c.border_subtle};
    color: {c.text_disabled};
}}

QPushButton[variant="ghost"]:disabled {{
    background-color: transparent;
    border-color: transparent;
    color: {c.text_disabled};
}}

/* A named secondary action. Outlined so it can sit beside the filled primary without
   competing with it, and without reading as destructive the way a red button would. */
QPushButton[variant="outline"] {{
    background-color: transparent;
    border: 1px solid {c.border_strong};
    color: {c.text};
    font-weight: {f.medium};
    padding: {s.sm}px {s.base}px;
}}

QPushButton[variant="outline"]:hover {{
    background-color: {c.surface_hover};
    border-color: {c.text_tertiary};
}}

QPushButton[variant="outline"]:pressed {{
    background-color: {c.surface_active};
}}

QPushButton[variant="outline"]:focus {{
    border-color: {c.accent};
}}

QPushButton[variant="outline"]:disabled {{
    color: {c.text_disabled};
    border-color: {c.border_subtle};
    background-color: transparent;
}}

QPushButton[variant="ghost"] {{
    background-color: transparent;
    border-color: transparent;
    color: {c.text_secondary};
}}

QPushButton[variant="ghost"]:hover {{
    background-color: {c.elevated};
    color: {c.text};
}}

QPushButton[variant="danger"] {{
    color: {c.danger};
    border-color: {with_alpha(c.danger, 0.4)};
    background-color: {c.danger_wash};
}}

QPushButton[variant="danger"]:hover {{
    background-color: {with_alpha(c.danger, 0.16)};
}}

/* Window controls: square, borderless, close goes red on hover like every Windows app. */
QPushButton#WindowButton {{
    background-color: transparent;
    border: none;
    border-radius: 0px;
    min-width: 44px;
    max-width: 44px;
    min-height: {z.titlebar}px;
    color: {c.text_secondary};
    font-size: {f.small}px;
}}

QPushButton#WindowButton:hover {{
    background-color: {c.overlay};
    color: {c.text};
}}

QPushButton#WindowButton[role="close"]:hover {{
    background-color: {c.danger};
    color: #FFFFFF;
}}

/* ────────────────────────── INPUTS ────────────────────────── */
QLineEdit, QTextEdit, QPlainTextEdit {{
    background-color: {c.base};
    color: {c.text};
    border: 1px solid {c.border};
    border-radius: {r.md}px;
    /* Vertical padding ADDS to min-height in Qt, so 8px here made every field 48px tall and
       a two-line settings row 110px. One grid step keeps a field at the shared control
       height. */
    padding: {s.xs}px {s.md}px;
    selection-background-color: {c.accent_muted};
    selection-color: {c.text};
    min-height: {z.control}px;
}}

QLineEdit:hover, QTextEdit:hover, QPlainTextEdit:hover {{
    border-color: {c.border_strong};
}}

/* One hairline, not a glowing box. A full-strength amber border on a resting empty field
   reads as an error state, and the composer's field is focused almost all the time. */
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {{
    border-color: {c.accent};
    background-color: {c.inset};
}}

QLineEdit:disabled {{
    color: {c.text_disabled};
    background-color: {c.surface};
}}

QLineEdit[secret="true"] {{
    font-family: {f.mono};
    letter-spacing: 2px;
}}

QComboBox {{
    background-color: {c.base};
    border: 1px solid {c.border};
    border-radius: {r.md}px;
    padding: {s.xs}px {s.md}px;
    min-height: {z.control}px;
    color: {c.text};
}}

QComboBox:hover {{ border-color: {c.border_strong}; }}
QComboBox:focus {{ border-color: {c.accent}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{ image: none; }}

QComboBox QAbstractItemView {{
    background-color: {c.overlay};
    border: 1px solid {c.border_strong};
    border-radius: {r.md}px;
    selection-background-color: {c.accent_wash};
    selection-color: {c.text};
    padding: {s.xs}px;
}}

QCheckBox {{ spacing: {s.sm}px; color: {c.text_secondary}; }}

QCheckBox::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {c.border_strong};
    border-radius: {r.sm}px;
    background-color: {c.inset};
}}

QCheckBox::indicator:checked {{
    background-color: {c.accent};
    border-color: {c.accent};
}}

QCheckBox::indicator:focus {{ border-color: {c.accent}; }}

QSlider::groove:horizontal {{
    height: 3px;
    background: {c.meter_track};
    border-radius: 2px;
}}

QSlider::handle:horizontal {{
    background: {c.accent};
    width: 12px; height: 12px;
    margin: -5px 0;
    border-radius: 6px;
}}

QSlider::sub-page:horizontal {{ background: {c.accent_muted}; border-radius: 2px; }}

/* ─────────────────────────── LISTS ────────────────────────── */
QListWidget, QTreeWidget, QTableWidget {{
    background-color: transparent;
    border: none;
    outline: none;
}}

QListWidget::item {{
    padding: {s.sm}px {s.md}px;
    border-radius: {r.md}px;
    color: {c.text_secondary};
}}

QListWidget::item:hover {{ background-color: {c.elevated}; color: {c.text}; }}
QListWidget::item:selected {{ background-color: {c.accent_wash}; color: {c.text}; }}

QHeaderView::section {{
    background-color: transparent;
    color: {c.text_tertiary};
    border: none;
    border-bottom: 1px solid {c.border_subtle};
    padding: {s.sm}px;
    font-size: {f.caption}px;
    font-weight: {f.semibold};
}}

/* ──────────────────────── SCROLLBARS ──────────────────────── */
/* Thin, no arrows, no track fill. A scrollbar is not information. */
QScrollArea {{ background-color: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background-color: transparent; }}

QScrollBar:vertical {{
    background: transparent;
    width: {z.scrollbar}px;
    margin: 0px;
}}

QScrollBar::handle:vertical {{
    background: {c.border};
    border-radius: {z.scrollbar // 2}px;
    min-height: 32px;
}}

QScrollBar::handle:vertical:hover {{ background: {c.border_strong}; }}

QScrollBar:horizontal {{
    background: transparent;
    height: {z.scrollbar}px;
}}

QScrollBar::handle:horizontal {{
    background: {c.border};
    border-radius: {z.scrollbar // 2}px;
    min-width: 32px;
}}

QScrollBar::add-line, QScrollBar::sub-line {{ height: 0px; width: 0px; border: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

/* ───────────────────────── PROGRESS ───────────────────────── */
QProgressBar {{
    background-color: {c.meter_track};
    border: none;
    border-radius: 2px;
    height: 4px;
    text-align: center;
    color: transparent;
}}

QProgressBar::chunk {{
    background-color: {c.accent};
    border-radius: 2px;
}}

/* ─────────────────────── STATUS PILLS ─────────────────────── */
/* A status is INFORMATION, not a control. The System page shows a column of them and they
   were being read as outlined buttons — same border, same radius, same height as the ghost
   buttons elsewhere. Squaring the radius and dropping the border weight separates the two
   families: rounded-with-a-border is something you press, this is something you read. */
#StatusPill {{
    /* An explicit half-height radius, not `pill` (999px). Qt does not clamp an oversized
       border-radius the way CSS does — it fell back to a small radius and every status chip
       in the app rendered as a rectangle. */
    border-radius: 11px;
    padding: 0px {s.md}px;
    min-height: 22px;
    max-height: 22px;
    font-size: {f.caption}px;
    font-weight: {f.medium};
    border: 1px solid transparent;
}}

#StatusPill[tone="success"] {{ color: {c.success}; background-color: {c.success_wash};
                               border-color: {c.success_edge}; }}
#StatusPill[tone="warning"] {{ color: {c.warning}; background-color: {c.warning_wash};
                               border-color: {c.warning_edge}; }}
#StatusPill[tone="danger"]  {{ color: {c.danger};  background-color: {c.danger_wash};
                               border-color: {c.danger_edge}; }}
#StatusPill[tone="accent"]  {{ color: {c.accent};  background-color: {c.accent_wash};
                               border-color: {with_alpha(c.accent, 0.28)}; }}
#StatusPill[tone="neutral"] {{ color: {c.text_secondary}; background-color: {c.overlay};
                               border-color: {c.border}; }}

/* ──────────────────────── CHAT ITEMS ──────────────────────── */
/* The user is on an amber-washed surface, Kayra on a neutral one. Asymmetry does the work that
   avatars usually do, at a fraction of the visual weight. */
#BubbleUser {{
    background-color: {c.accent_wash};
    border: 1px solid {with_alpha(c.accent, 0.22)};
    border-radius: {r.lg}px;
    padding: {s.md}px {s.base}px;
}}

/* A reply that is still arriving carries an accent edge. Painted by a property selector, so
   marking it costs one repolish and no timer — see `MessageRow.set_streaming`. */
#BubbleAssistant[streaming="true"] {{
    border-left: 2px solid {c.accent};
}}

#BubbleAssistant {{
    background-color: {c.elevated};
    border: 1px solid {c.border_subtle};
    border-radius: {r.lg}px;
    padding: {s.md}px {s.base}px;
}}

#BubbleSystem {{
    background-color: transparent;
    border: 1px solid {c.border_subtle};
    border-radius: {r.md}px;
    padding: {s.sm}px {s.md}px;
}}

#BubbleError {{
    background-color: {c.danger_wash};
    border: 1px solid {with_alpha(c.danger, 0.32)};
    border-radius: {r.md}px;
    padding: {s.sm}px {s.md}px;
}}

#AutomationTrace {{
    background-color: {c.copper_wash};
    border: 1px solid {with_alpha(c.copper, 0.26)};
    border-radius: {r.md}px;
    padding: {s.sm}px {s.md}px;
}}

/* ───────────────────────── AMBIENT ────────────────────────── */
#AmbientSurface {{
    background-color: {c.surface};
    border: 1px solid {c.border_strong};
    border-radius: {r.xl}px;
}}

/* ──────────────────────── SETTINGS ────────────────────────── */
#SettingRow {{
    border-bottom: 1px solid {c.border_subtle};
}}

/* THE ONE RULE THIS SCREEN NEEDS: the title outranks the description, in weight AND colour.
   They were the same weight two colours apart, so a settings row read as one grey block and
   the eye had to parse it rather than scan it. */
#SettingName {{
    font-size: {f.body}px;
    font-weight: {f.medium};
    color: {c.text};
}}

#SettingHelp {{
    font-size: {f.small}px;
    color: {c.text_tertiary};
}}


/* ─────────────────── SEGMENTED CONTROL ─────────────────── */
/* One control, several exclusive options — filters, view switches. Rendered as a single
   inset track with the selected segment lifted out of it, rather than as four separate
   buttons, which is what the Activity filters used to look like and why they read as four
   unrelated actions. */
#Segmented {{
    background-color: {c.surface};
    border: 1px solid {c.border_subtle};
    border-radius: {r.md}px;
    padding: 3px;
}}

QPushButton#SegmentedItem {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: {r.sm}px;
    padding: 0px {s.md}px;
    min-height: 26px;
    max-height: 26px;
    color: {c.text_secondary};
    font-size: {f.small}px;
    font-weight: {f.regular};
}}

QPushButton#SegmentedItem:hover {{
    color: {c.text};
    background-color: {c.surface_hover};
}}

QPushButton#SegmentedItem:checked {{
    background-color: {c.elevated};
    border-color: {c.border};
    color: {c.text};
    font-weight: {f.medium};
}}

QPushButton#SegmentedItem:focus {{
    border-color: {c.accent_subtle};
}}

/* ───────────────────── LIST ROWS ───────────────────────── */
/* Rows in a timeline, an action history, a memory list. A hover that is felt rather than
   seen: one step up the surface ladder, no border, no movement. */
#ListRow {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: {r.md}px;
}}

#ListRow:hover {{
    background-color: {c.surface_hover};
}}

#ListRow[selected="true"] {{
    background-color: {c.accent_wash};
    border-color: {c.accent_subtle};
}}

/* A quiet divider between rows in a dense list — lighter than #Divider, which is for
   separating whole regions. */
#RowRule {{
    background-color: {c.border_subtle};
    max-height: 1px;
    min-height: 1px;
    border: none;
}}

/* ───────────────────── SETTINGS ────────────────────────── */
/* Compact by design. A settings screen where each row is a 110px card is a screen you
   scroll instead of read. */
#SettingsGroup {{
    background-color: {c.elevated};
    border: 1px solid {c.border_subtle};
    border-radius: {r.lg}px;
}}

#SettingsGroupHeader {{
    background-color: transparent;
    border-bottom: 1px solid {c.border_subtle};
}}

/* ──────────────────── DISCLOSURE ───────────────────────── */
/* An expandable section: the System guide's questions, Settings' advanced block. */
QPushButton#Disclosure {{
    background-color: transparent;
    border: none;
    border-radius: {r.md}px;
    padding: {s.sm}px {s.sm}px;
    text-align: left;
    color: {c.text};
    font-size: {f.body}px;
    font-weight: {f.medium};
    min-height: 30px;
}}

QPushButton#Disclosure:hover {{
    background-color: {c.surface_hover};
}}

QPushButton#Disclosure:checked {{
    color: {c.accent};
}}

QPushButton#Disclosure:focus {{
    background-color: {c.surface_hover};
}}

/* ───────────────────── ACTION STATUS ───────────────────── */
/* The outcome of one automation action. Every variant has identical metrics — a success and
   a failure that differ in width make a list of them look ragged and make the eye measure
   length instead of reading colour. The mark on the left is DRAWN, so the state survives a
   monochrome screen and does not depend on a glyph existing in the font. */
#ActionStatus {{
    border-radius: {r.sm}px;
    border: 1px solid {c.border};
    background-color: {c.overlay};
}}

#ActionStatus[tone="success"] {{ background-color: {c.success_wash};
                                 border-color: {c.success_edge}; }}
#ActionStatus[tone="danger"]  {{ background-color: {c.danger_wash};
                                 border-color: {c.danger_edge}; }}
#ActionStatus[tone="warning"] {{ background-color: {c.warning_wash};
                                 border-color: {c.warning_edge}; }}
#ActionStatus[tone="neutral"] {{ background-color: {c.overlay};
                                 border-color: {c.border}; }}

#ActionStatusLabel {{
    font-size: {f.caption}px;
    font-weight: {f.semibold};
    color: {c.text_secondary};
}}

#ActionStatus[tone="success"] #ActionStatusLabel {{ color: {c.success}; }}
#ActionStatus[tone="danger"]  #ActionStatusLabel {{ color: {c.danger}; }}
#ActionStatus[tone="warning"] #ActionStatusLabel {{ color: {c.warning}; }}

/* ──────────────────── CARD HEADER ACTION ───────────────── */
/* A control that sits on a card's title row beside a status pill. It has to match the pill's
   height or the header looks broken — a 22px pill next to a 40px button was the most obvious
   alignment fault in the consistency audit (Memory's "Clear all", Activity's "Clear"). */
QPushButton#CardAction {{
    background-color: transparent;
    border: 1px solid {c.border};
    border-radius: 11px;
    padding: 0px {s.md}px;
    min-height: 22px;
    max-height: 22px;
    font-size: {f.caption}px;
    font-weight: {f.medium};
    color: {c.text_secondary};
}}

QPushButton#CardAction:hover {{
    background-color: {c.surface_hover};
    border-color: {c.border_strong};
    color: {c.text};
}}

QPushButton#CardAction[tone="danger"] {{
    color: {c.danger};
    border-color: {with_alpha(c.danger, 0.35)};
}}

QPushButton#CardAction[tone="danger"]:hover {{
    background-color: {c.danger_wash};
    border-color: {c.danger};
}}

/* ─────────────────────── TOGGLE ────────────────────────── */
/* The switch paints itself entirely in paintEvent. All this rule does is stop the generic
   QPushButton metrics from resizing it — Qt's stylesheet min-height overrides setFixedSize. */
QPushButton#Toggle {{
    background: transparent;
    border: none;
    padding: 0px;
    margin: 0px;
    min-width: 40px;
    max-width: 40px;
    min-height: 22px;
    max-height: 22px;
}}

/* ───────────────────── ICON BUTTON ─────────────────────── */
/* A square control carrying a drawn glyph and no text: the microphone, a row's remove
   action. Same height as every other control so it can sit in any row. */
QPushButton#IconButton {{
    background-color: {c.overlay};
    border: 1px solid {c.border};
    border-radius: {r.md}px;
    padding: 0px;
    min-width: {z.control}px;
    max-width: {z.control}px;
    min-height: {z.control}px;
    max-height: {z.control}px;
}}

QPushButton#IconButton:hover {{
    background-color: {c.surface_hover};
    border-color: {c.border_strong};
}}

QPushButton#IconButton:checked,
QPushButton#IconButton[listening="true"] {{
    background-color: {c.accent_wash};
    border-color: {c.accent};
}}

/* A paused microphone is marked, not merely un-highlighted: an absent highlight is
   indistinguishable from "idle but listening". */
QPushButton#IconButton[paused="true"] {{
    background-color: {c.danger_wash};
    border-color: {c.danger_edge};
}}

QPushButton#IconButton:disabled {{
    background-color: transparent;
    border-color: {c.border_subtle};
}}

/* ─────────────────── CHAT COMPOSER ─────────────────────── */
/* The composer is anchored and reads as one object: a bar containing the field and its
   controls, rather than three widgets that happen to be adjacent. */
#Composer {{
    background-color: {c.surface};
    border: 1px solid {c.border};
    border-radius: {r.lg}px;
}}

#Composer[focused="true"] {{
    border-color: {c.accent_subtle};
}}

#Composer QLineEdit {{
    background-color: transparent;
    border: none;
    min-height: {z.control}px;
    max-height: {z.control}px;
}}

/* Every control inside the composer is exactly one control-height. Without the max, the
   Send button stretched to the bar's full height and stood a head taller than the
   microphone beside it. */
#Composer QPushButton {{
    min-height: {z.control}px;
    max-height: {z.control}px;
}}

#Composer QLineEdit:focus {{
    background-color: transparent;
    border: none;
}}

/* Bubble metadata: the time and the "spoken" marker, inside the bubble where they belong
   rather than floating beside it. */
#BubbleMeta {{
    font-size: {f.micro}px;
    color: {c.text_tertiary};
}}

/* A day/▸session separator in the transcript. */
#TranscriptRule {{
    font-size: {f.micro}px;
    color: {c.text_tertiary};
    letter-spacing: {f.tracking_label}px;
}}
"""
