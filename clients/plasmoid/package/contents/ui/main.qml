/*
 * TuxThrottle plasmoid — reads `tuxthrottlectl status --json` on a timer and
 * shows CPU/GPU temps in the panel with a click-to-switch power profile.
 * Read-only except the two profile buttons: they run
 * `pkexec /usr/local/bin/tuxthrottlectl set power-profile ...`. The
 * PolkitTuxthrottlectl tweak makes that passwordless for an active local
 * user; without it (or the tuxthrottled control socket) pkexec shows a
 * normal auth dialog.
 */
import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.components 3.0 as PC
import org.kde.plasma.plasma5support 2.0 as P5Support
import org.kde.kirigami 2.20 as Kirigami

PlasmoidItem {
    id: root

    readonly property string ctl: "/opt/tuxthrottle/tuxthrottlectl.py"
    property var st: ({})
    property bool reachable: false

    function num(v) { return (typeof v === "number") ? Math.round(v) : null }
    function cpuT() { return st.cpu ? num(st.cpu.temp_c) : null }
    function gpuT() { return st.dgpu ? num(st.dgpu.temp_c) : null }
    function prof() { return st.platform_profile || "?" }
    function hottest() {
        var xs = [cpuT(), gpuT()].filter(function (x) { return x !== null })
        return xs.length ? Math.max.apply(null, xs) : 0
    }

    P5Support.DataSource {
        id: exec
        engine: "executable"
        connectedSources: []
        onNewData: function (source, data) {
            exec.disconnectSource(source)
            if (data["exit code"] === 0 && data.stdout) {
                try {
                    root.st = JSON.parse(data.stdout)
                    root.reachable = true
                } catch (e) { root.reachable = false }
            } else if (source.indexOf("status --json") !== -1) {
                root.reachable = false
            } else {
                poll.restart()   // a `set` finished — refresh now
            }
        }
        function run(cmd) { connectSource(cmd) }
    }

    Timer {
        id: poll
        interval: 5000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: exec.run("python3 " + root.ctl + " status --json")
    }

    function setProfile(p) {
        // pkexec against the installed launcher — path must match the
        // PolkitTuxthrottlectl action's exec.path annotation exactly.
        exec.run("pkexec /usr/local/bin/tuxthrottlectl set power-profile " + p)
    }

    toolTipMainText: "TuxThrottle"
    toolTipSubText: reachable
        ? ("profile: " + prof()
           + (cpuT() !== null ? "\nCPU: " + cpuT() + " °C" : "")
           + (gpuT() !== null ? "\ndGPU: " + gpuT() + " °C" : ""))
        : "tuxthrottlectl not reachable"

    compactRepresentation: MouseArea {
        onClicked: root.expanded = !root.expanded
        RowLayout {
            anchors.fill: parent
            spacing: Kirigami.Units.smallSpacing
            Kirigami.Icon {
                source: "tuxthrottle"
                fallback: "temperature-normal"
                Layout.fillHeight: true
                Layout.preferredWidth: height
            }
            PC.Label {
                text: root.reachable && root.cpuT() !== null
                      ? root.cpuT() + "°" : "–"
                font.pointsize: Kirigami.Theme.smallFont.pointSize
            }
        }
    }

    fullRepresentation: ColumnLayout {
        Layout.minimumWidth: Kirigami.Units.gridUnit * 14
        Layout.minimumHeight: Kirigami.Units.gridUnit * 10
        spacing: Kirigami.Units.smallSpacing

        Kirigami.Heading { level: 3; text: "TuxThrottle" }

        PC.Label {
            visible: !root.reachable
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            text: "Can't reach tuxthrottlectl at\n" + root.ctl
        }

        GridLayout {
            visible: root.reachable
            columns: 2
            columnSpacing: Kirigami.Units.largeSpacing
            PC.Label { text: "Profile" }
            PC.Label { text: root.prof() }
            PC.Label { text: "CPU" }
            PC.Label { text: root.cpuT() !== null ? root.cpuT() + " °C" : "n/a" }
            PC.Label { text: "dGPU" }
            PC.Label { text: root.gpuT() !== null ? root.gpuT() + " °C" : "n/a" }
        }

        RowLayout {
            visible: root.reachable
            Layout.fillWidth: true
            PC.Button {
                Layout.fillWidth: true
                text: "Balanced"
                onClicked: root.setProfile("balanced")
            }
            PC.Button {
                Layout.fillWidth: true
                text: "Performance"
                onClicked: root.setProfile("performance")
            }
        }
    }
}
