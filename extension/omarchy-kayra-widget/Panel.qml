import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Kayra status pill for the Omarchy bar. Reads $XDG_RUNTIME_DIR/kayra/status.json through
// status_stream.py (same streaming pattern as Omarchy's own system-stats widgets).
// Left click: bring Kayra's window forward, or start Kayra if it is not running.
Panel {
  id: root
  moduleName: "kayra.status"
  ipcTarget: "kayra.status"
  manageIpc: true
  visible: true
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  property string state: "OFFLINE"
  property bool listening: false
  property bool sleeping: false
  readonly property string here: Quickshell.env("HOME") + "/.config/omarchy/plugins/kayra.status"
  readonly property string kayraDir: Quickshell.env("KAYRA_HOME") || (Quickshell.env("HOME") + "/Documents/project-kayra")

  readonly property string label: {
    if (state === "OFFLINE") return "󰚩 off"
    if (sleeping) return "󰒲 standby"
    if (state === "SPEAKING") return "󰚩 speaking"
    if (state === "PROCESSING" || state === "AUTOMATING") return "󰚩 thinking"
    if (!listening) return "󰍭 paused"
    return "󰚩 listening"
  }

  Process {
    id: stream
    command: ["python3", root.here + "/status_stream.py"]
    running: true
    stdout: SplitParser {
      onRead: function(line) {
        try {
          var d = JSON.parse(String(line))
          root.state = d.state || "OFFLINE"
          root.listening = !!d.listening
          root.sleeping = !!d.sleeping
        } catch (e) {}
      }
    }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.label
    tooltipText: root.state === "OFFLINE" ? "Kayra is not running · click to start it"
                                          : "Kayra · click to bring it up"
    onPressed: function(b) {
      if (root.state === "OFFLINE") {
        Quickshell.execDetached(["uwsm-app", "--", "bash", "-c", "cd '" + root.kayraDir + "' && exec python3 run.py"])
      } else {
        Quickshell.execDetached(["omarchy-hyprland-focus-app", "^kayra$"])
      }
    }
  }
}
