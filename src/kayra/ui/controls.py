# ┌────────────────────────────────────────────────────────────────────────┐
# │                            controls.py                                 │
# │            The Actions Behind Kayra's Controls, In One Place            │
# └────────────────────────────────────────────────────────────────────────┘
"""
What a press DOES, separated from where the press happened.

WHY THIS EXISTS
---------------
The same four actions are reachable from several surfaces — the floating dock on Home and
Chat, the tray, the keyboard shortcuts, and Settings. Before this they were implemented on
whichever screen happened to own the button, and a second surface wanting the same action had
to either call into that screen or grow its own copy. The copies then drift: one re-reads the
backend after a failed toggle and the other does not, and the screen that does not is the one
that ends up showing a state the machine never adopted.

So the actions live here, once, and every surface calls them.

WHAT IT IS NOT
--------------
It is not a state store. Nothing is cached, nothing is remembered, and there is no "current"
anything. Each method asks the bridge what is true, acts, and hands back what actually
happened. A control that remembered what it last requested is precisely the UI-only fake
state this codebase refuses everywhere else.

It is also not a teardown. `confirm_shutdown` asks the user and then delegates to
`bridge.shutdown(hard=True)`, which is `app.request_shutdown` — the one authoritative path.
There is no second ordering here and there must never be.
"""

from PySide6.QtWidgets import QMessageBox


class KayraControls:
    """
    The action layer between a control and the bridge.

    Holds a bridge and nothing else. Constructed once by the window; safe to construct in a
    test with a stub.
    """

    def __init__(self, bridge, parent_widget=None):
        self.bridge = bridge
        # Only used to parent the confirmation dialog, so it centres on the window and is
        # modal to it rather than to the whole application.
        self.parent_widget = parent_widget

    # ──────────────────────────────────────────────────────────────────
    #                            LISTENING
    # ──────────────────────────────────────────────────────────────────

    def toggle_listening(self):
        """
        Opens or closes the microphone.

        Reads the runtime rather than a local flag, because the state may have been changed
        by the spoken "stop listening", by the tray, or by the other window. THE MICROPHONE
        AND "LISTENING" ARE ONE FACT — there is deliberately no second method here for a
        separate "mic mute", because there is no separate thing to mute.
        """
        return self.bridge.set_listening(not self.bridge.listening_enabled())

    # ──────────────────────────────────────────────────────────────────
    #                       CAMERA AND GESTURES
    # ──────────────────────────────────────────────────────────────────

    def toggle_camera(self):
        """
        Turns the camera on or off, then reports what ACTUALLY happened.

        Returns `(ok, detail)` unchanged from the bridge. The caller re-reads
        `gesture_status()` afterwards rather than trusting this return, which is the rule
        that keeps a failed switch — the camera is unplugged, another application holds it —
        from leaving a control showing the state the user asked for.
        """
        status = self.bridge.gesture_status() or {}
        camera_on = str(status.get("camera", "OFF")) not in ("OFF", "ERROR")
        return self.bridge.set_camera(not camera_on)

    def toggle_gesture(self):
        """
        Turns hand gesture control on or off.

        Enabling starts the camera first when it is off — THE CONTROLLER does that, not this
        layer. Sequencing "camera, then detector, then pointer" here would be a second
        implementation of the one operation that must not have two, and it would drift the
        moment the ordering changed.
        """
        status = self.bridge.gesture_status() or {}
        return self.bridge.set_gesture(not bool(status.get("gesture_enabled")))

    # ──────────────────────────────────────────────────────────────────
    #                             SHUTDOWN
    # ──────────────────────────────────────────────────────────────────

    def confirm_shutdown(self):
        """
        Asks, and only then hands over to the ONE authoritative shutdown path.

        Returns True when the user confirmed and teardown has been requested.

        THIS DOES NOT SHUT DOWN WINDOWS, and the dialog says so. A Windows power action is a
        different thing entirely, with its own confirmation, owned by the automation policy —
        nothing on this path can reach it.

        The caller disables its controls on a True return and never re-enables them: teardown
        takes a couple of seconds (the browser session has nine processes to reap), and a
        second press during that window is the easiest way to re-enter a shutdown that is
        already half done.
        """
        box = QMessageBox(self.parent_widget)
        box.setWindowTitle("Shut down Kayra")
        box.setText("Shut down Kayra?")
        box.setInformativeText(
            "Voice input, speech and every background service stop, and the window closes. "
            "This does not shut down your computer.")
        box.setStandardButtons(QMessageBox.Cancel | QMessageBox.Yes)
        box.setDefaultButton(QMessageBox.Cancel)
        if box.exec() != QMessageBox.Yes:
            return False
        self.bridge.shutdown(hard=True)
        return True
