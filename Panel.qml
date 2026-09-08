import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "digital.1001.gopro"
  ipcTarget: "gopro"
  manageIpc: false

  property string focusSection: "primary"
  property int dayIndex: 0
  property bool cursorActive: false

  // Confirmation state for the two destructive actions. Nothing is ever sent
  // to the camera from here without one of these being answered first.
  property bool confirmOpen: false
  property string confirmKind: ""
  property string confirmDay: ""

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property var days: gopro.status.days || []
  readonly property var totals: gopro.status.totals || ({})
  readonly property bool hideWhenDisconnected: gopro.boolSetting("hideWhenDisconnected", true)

  readonly property string confirmMessage: {
    if (confirmKind === "erase")
      return "Erase the card? " + Model.formatBytes(totals.bytes)
             + " across " + (totals.files || 0) + " file(s) is verified on disk and will be "
             + "removed from the camera. This cannot be undone."
    if (confirmKind === "day") {
      var day = dayByKey(confirmDay)
      return "Remove " + Model.dayLabel(confirmDay) + " from the camera? "
             + (day ? day.count + " file(s), " + Model.formatBytes(day.bytes) : "")
             + " stays on disk. This cannot be undone."
    }
    return ""
  }

  function dayByKey(key) {
    for (var i = 0; i < days.length; i++) if (days[i].day === key) return days[i]
    return null
  }

  function askErase() {
    if (!Model.canErase(gopro.status)) {
      gopro.lastError = Model.eraseBlockedReason(gopro.status)
      return
    }
    confirmKind = "erase"
    confirmDay = ""
    confirmDialog.selectedIndex = 0
    confirmOpen = true
  }

  function askDeleteDay(day) {
    var entry = dayByKey(day)
    if (!entry || !entry.complete) {
      gopro.lastError = "Copy this day to disk before removing it from the camera."
      return
    }
    confirmKind = "day"
    confirmDay = day
    confirmDialog.selectedIndex = 0
    confirmOpen = true
  }

  function acceptConfirm() {
    if (confirmKind === "erase") gopro.eraseCard()
    else if (confirmKind === "day") gopro.deleteDay(confirmDay)
    dismissConfirm()
  }

  function dismissConfirm() {
    confirmOpen = false
    confirmKind = ""
    confirmDay = ""
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  function primaryAction() {
    if (gopro.syncing) gopro.cancel()
    else if (gopro.connected) gopro.syncAll()
    else gopro.refresh()
  }

  function ensureCursor() {
    if (days.length === 0) {
      focusSection = "primary"
      dayIndex = 0
      return
    }
    if (dayIndex >= days.length) dayIndex = days.length - 1
    if (dayIndex < 0) dayIndex = 0
  }

  function moveCursor(dx, dy) {
    cursorActive = true
    ensureCursor()
    if (dy === 0) return
    if (focusSection === "primary") {
      if (dy > 0 && days.length > 0) { focusSection = "days"; dayIndex = 0; scrollCursorIntoView() }
      return
    }
    if (dy < 0 && dayIndex === 0) {
      focusSection = "primary"
      if (panelFlick) panelFlick.contentY = 0
      return
    }
    dayIndex = Math.max(0, Math.min(days.length - 1, dayIndex + dy))
    scrollCursorIntoView()
  }

  function activateCursor() {
    ensureCursor()
    if (focusSection === "primary") primaryAction()
    else if (focusSection === "days") {
      var day = days[dayIndex]
      if (!day) return
      if (day.complete) gopro.openDay(day.day)
      else gopro.syncDay(day.day)
    }
  }

  function scrollItemIntoView(item) {
    if (!panelFlick || !item) return
    Qt.callLater(function() {
      if (!item) return
      var margin = Style.space(6)
      var point = item.mapToItem(panelFlick.contentItem, 0, 0)
      var top = point.y
      var bottom = top + item.height
      var maxY = Math.max(0, panelFlick.contentHeight - panelFlick.height)
      if (top < panelFlick.contentY + margin) panelFlick.contentY = Math.max(0, top - margin)
      else if (bottom > panelFlick.contentY + panelFlick.height - margin)
        panelFlick.contentY = Math.min(maxY, bottom + margin - panelFlick.height)
    })
  }

  function scrollCursorIntoView() {
    if (focusSection === "days" && dayColumn && dayIndex >= 0 && dayIndex < dayColumn.children.length)
      scrollItemIntoView(dayColumn.children[dayIndex])
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight
  visible: !hideWhenDisconnected || gopro.connected || gopro.syncing

  onOpenedChanged: {
    // Closing must never leave a confirmation armed for the next open.
    confirmOpen = false
    confirmKind = ""
    confirmDay = ""
    if (!opened) return
    cursorActive = false
    if (panelFlick) panelFlick.contentY = 0
    gopro.refresh()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }
  onDayIndexChanged: scrollCursorIntoView()
  onConfirmOpenChanged: if (confirmOpen) Qt.callLater(function() { confirmKeys.forceActiveFocus() })

  Service {
    id: gopro
    settings: root.settings
  }

  Connections {
    target: gopro
    function onStatusChanged() { root.ensureCursor() }
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { gopro.refresh(); return "ok" }
    function sync(): string { gopro.syncAll(); return "ok" }
    function cancel(): string { gopro.cancel(); return "ok" }
    function status(): string { return Model.jobHeadline(gopro.job, gopro.status) }
    // Opens the confirmation; it never erases anything on its own.
    function erase(): string {
      if (!Model.canErase(gopro.status)) return Model.eraseBlockedReason(gopro.status)
      root.open()
      // After the open, so opening the panel does not clear the confirmation.
      Qt.callLater(function() { root.askErase() })
      return "confirm"
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    iconComponent: Component {
      Item {
        GoProIcon {
          id: barIcon
          anchors.centerIn: parent
          iconSize: Style.space(11)
          color: gopro.connected ? root.barForeground : Qt.darker(root.barForeground, 1.6)
        }

        // A hairline under the icon while a transfer runs: the bar slot is a
        // fixed square, so there is no room for a percentage, but a filling
        // line reads at a glance.
        Rectangle {
          visible: gopro.syncing
          anchors.horizontalCenter: parent.horizontalCenter
          anchors.top: barIcon.bottom
          anchors.topMargin: Style.space(2)
          width: barIcon.width
          height: Math.max(1, Style.space(1.5))
          radius: height / 2
          color: Qt.darker(root.barForeground, 2.2)

          Rectangle {
            anchors.left: parent.left
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            width: parent.width * Model.jobProgress(gopro.job)
            radius: parent.radius
            color: root.barForeground
            Behavior on width { NumberAnimation { duration: 400; easing.type: Easing.OutQuad } }
          }
        }
      }
    }
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) gopro.refresh()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(420))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(620))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: root.confirmOpen
      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        root.moveCursor(dx, dy)
      }
      onActivateRequested: if (root.cursorActive) root.activateCursor()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        var key = String(t || "").toLowerCase()
        if (key === "r") gopro.refresh()
        else if (key === "s") { if (!gopro.syncing && gopro.connected) gopro.syncAll() }
        else if (key === "x") { if (gopro.syncing) gopro.cancel() }
        else if (key === "o") gopro.openArchive("")
        else if (key === "e") root.askErase()
      }

      // Takes focus only while a confirmation is up, so Enter and Escape
      // answer the dialog instead of driving the list behind it.
      Item {
        id: confirmKeys
        anchors.fill: parent
        Keys.onPressed: function(event) {
          if (confirmDialog.handleKey(event)) event.accepted = true
        }
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(12)

          PanelHero {
            id: hero
            width: parent.width
            title: gopro.connected ? (gopro.status.model || "GoPro") : "GoPro"
            meta: Model.jobHeadline(gopro.job, gopro.status)
            foreground: root.foreground
            fontFamily: root.fontFamily
            iconOpacity: gopro.connected ? 1.0 : 0.5
            iconComponent: Component {
              GoProIcon {
                iconSize: Style.font.display
                color: root.foreground
              }
            }
            trailingControl: Component {
              Text {
                textFormat: Text.PlainText
                visible: gopro.connected && gopro.status.battery >= 0
                text: Model.batteryGlyph(gopro.status.battery) + " " + gopro.status.battery + "%"
                color: gopro.status.battery <= 15 ? root.urgent : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
              }
            }
          }

          // What the camera is doing, or why it isn't reachable.
          Text {
            textFormat: Text.PlainText
            visible: text !== ""
            width: parent.width
            text: gopro.lastError !== "" ? gopro.lastError : gopro.actionStatus
            color: gopro.lastError !== "" ? root.urgent : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          // The failure mode that costs people the most time: the camera is
          // plugged in and charging, so it looks fine, but its USB mode makes
          // it invisible. Say exactly which setting to change.
          Column {
            visible: !gopro.connected
            width: parent.width
            spacing: Style.space(6)

            Text {
              textFormat: Text.PlainText
              width: parent.width
              text: gopro.status.reason === "no-http"
                ? "Camera found, but it is not answering."
                : "No GoPro connected."
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              wrapMode: Text.WordWrap
            }

            Text {
              textFormat: Text.PlainText
              width: parent.width
              text: gopro.status.reason === "no-http"
                ? "On the camera: Preferences → Connections → USB Connection → GoPro Connect. MTP mode turns this off."
                : "Connect a GoPro over USB and turn it on."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
          }

          // Live transfer.
          Column {
            visible: gopro.syncing
            width: parent.width
            spacing: Style.space(6)

            ProgressTrack {
              width: parent.width
              fraction: Model.jobProgress(gopro.job)
            }

            RowLayout {
              width: parent.width
              spacing: Style.space(8)

              Text {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                text: gopro.job && gopro.job.currentFile ? gopro.job.currentFile : ""
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideMiddle
              }

              Text {
                textFormat: Text.PlainText
                text: Model.jobDetail(gopro.job, gopro.status)
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
          }

          // A card that changed underneath a running job, or files that went
          // missing before they were copied. Both have really happened; both
          // are silent unless something says so.
          Text {
            textFormat: Text.PlainText
            visible: gopro.job && Array.isArray(gopro.job.warnings) && gopro.job.warnings.length > 0
            width: parent.width
            text: gopro.job && gopro.job.warnings ? gopro.job.warnings.join(" ") : ""
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Column {
            visible: gopro.connected
            width: parent.width
            spacing: Style.spacing.labelGap

            InfoPair {
              label: "On the card"
              value: (root.totals.files || 0) + " files · " + Model.formatBytes(root.totals.bytes)
            }
            InfoPair {
              label: "Still to copy"
              value: Number(root.totals.pendingFiles || 0) === 0
                ? "Nothing"
                : root.totals.pendingFiles + " · " + Model.formatBytes(root.totals.pendingBytes)
                  + " · ~" + Model.estimateDuration(root.totals.pendingBytes)
            }
            InfoPair {
              label: "Card space"
              value: Model.cardText(gopro.status)
              urgentValue: Model.cardNearlyFull(gopro.status)
            }
          }

          PanelSeparator { visible: gopro.connected; foreground: root.foreground }

          Column {
            visible: gopro.connected
            width: parent.width
            spacing: Style.space(10)

            PanelSectionHeader {
              text: "BY DAY"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Text {
              textFormat: Text.PlainText
              visible: root.days.length === 0
              width: parent.width
              text: "The card is empty."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              horizontalAlignment: Text.AlignHCenter
            }

            Column {
              id: dayColumn
              width: parent.width
              spacing: Style.space(6)

              Repeater {
                model: root.days
                DayRow {
                  required property var modelData
                  required property int index
                  width: dayColumn.width
                  day: modelData
                  rowIndex: index
                }
              }
            }
          }

          PanelSeparator { visible: gopro.connected; foreground: root.foreground }

          RowLayout {
            width: parent.width
            spacing: Style.space(8)

            ActionButton {
              Layout.fillWidth: true
              hasCursor: root.cursorActive && root.focusSection === "primary"
              iconText: gopro.syncing ? "󰅖" : "󰇚"
              title: gopro.syncing ? "Stop copying"
                                   : (Number(root.totals.pendingFiles || 0) === 0
                                      ? "Everything is copied" : "Copy everything")
              subtitle: gopro.syncing
                ? Model.jobDetail(gopro.job, gopro.status)
                : (Number(root.totals.pendingFiles || 0) === 0
                   ? String(gopro.status.dest || "")
                   : Model.formatBytes(root.totals.pendingBytes) + " · about "
                     + Model.estimateDuration(root.totals.pendingBytes))
              enabled: gopro.connected && (gopro.syncing || Number(root.totals.pendingFiles || 0) > 0)
              onTriggered: root.primaryAction()
              onHovered: { root.cursorActive = true; root.focusSection = "primary" }
            }

            PanelActionButton {
              iconText: "󰉋"
              foreground: root.foreground
              fontFamily: root.fontFamily
              Layout.alignment: Qt.AlignVCenter
              tooltipText: "Open " + (gopro.status.dest || "the archive")
              onClicked: gopro.openArchive("")
            }
          }

          // Erase stays visible but inert until every file is verified on
          // disk, so the reason it is unavailable is legible rather than the
          // button simply being absent.
          Column {
            visible: gopro.connected && Number(root.totals.files || 0) > 0
            width: parent.width
            spacing: Style.space(4)

            ActionButton {
              width: parent.width
              danger: true
              iconText: "󰩹"
              title: "Erase the card"
              subtitle: Model.canErase(gopro.status)
                ? "Frees " + Model.formatBytes(root.totals.bytes) + " on the camera"
                : Model.eraseBlockedReason(gopro.status)
              enabled: Model.canErase(gopro.status) && !gopro.syncing
              onTriggered: root.askErase()
            }
          }
        }
      }
    }

    ConfirmDialog {
      id: confirmDialog
      anchors.fill: parent
      z: 20
      opened: root.confirmOpen
      message: root.confirmMessage
      confirmText: root.confirmKind === "erase" ? "Erase" : "Remove"
      cancelText: "Keep"
      background: Color.background
      foreground: root.foreground
      onConfirmed: root.acceptConfirm()
      onCanceled: root.dismissConfirm()
    }
  }

  // ------------------------------------------------------------- components

  component ProgressTrack: Rectangle {
    property real fraction: 0

    height: Style.space(4)
    radius: height / 2
    color: Qt.darker(root.foreground, 3.2)

    Rectangle {
      anchors.left: parent.left
      anchors.top: parent.top
      anchors.bottom: parent.bottom
      width: parent.width * Math.max(0, Math.min(1, parent.fraction))
      radius: parent.radius
      color: root.foreground
      Behavior on width { NumberAnimation { duration: 400; easing.type: Easing.OutQuad } }
    }
  }

  component ActionButton: CursorSurface {
    id: actionButton
    property string iconText: ""
    property string title: ""
    property string subtitle: ""
    property bool enabled: true
    property bool danger: false
    property alias containsMouse: actionMouse.containsMouse

    signal triggered()
    signal hovered()

    foreground: root.foreground
    opacity: enabled ? 1.0 : 0.45
    implicitHeight: actionContent.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      id: actionMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: actionButton.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
      onEntered: actionButton.hovered()
      onClicked: if (actionButton.enabled) actionButton.triggered()
    }

    RowLayout {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(8)

      Text {
        textFormat: Text.PlainText
        text: actionButton.iconText
        color: actionButton.danger ? root.urgent : root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.icon
        Layout.alignment: Qt.AlignVCenter
      }

      ColumnLayout {
        id: actionContent
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: actionButton.title
          color: actionButton.danger ? root.urgent : root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }

        Text {
          textFormat: Text.PlainText
          visible: text !== ""
          Layout.fillWidth: true
          text: actionButton.subtitle
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }
    }
  }

  component DayRow: CursorSurface {
    id: dayRow
    property var day: null
    property int rowIndex: 0
    readonly property string dayKey: day ? String(day.day || "") : ""
    readonly property bool complete: day ? day.complete === true : false

    hasCursor: root.cursorActive && root.focusSection === "days" && root.dayIndex === rowIndex
    foreground: root.foreground
    implicitHeight: dayContent.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onEntered: {
        root.cursorActive = true
        root.focusSection = "days"
        root.dayIndex = dayRow.rowIndex
      }
      onClicked: {
        if (dayRow.complete) gopro.openDay(dayRow.dayKey)
        else gopro.syncDay(dayRow.dayKey)
      }
    }

    ColumnLayout {
      id: dayContent
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(5)

      RowLayout {
        Layout.fillWidth: true
        spacing: Style.space(8)

        ColumnLayout {
          Layout.fillWidth: true
          spacing: Style.space(1)

          Text {
            textFormat: Text.PlainText
            Layout.fillWidth: true
            text: Model.dayLabel(dayRow.dayKey)
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            elide: Text.ElideRight
          }

          // The contents may elide; the status may not.
          RowLayout {
            Layout.fillWidth: true
            spacing: Style.space(5)

            Text {
              textFormat: Text.PlainText
              Layout.fillWidth: true
              text: Model.dayMeta(dayRow.day)
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
            }

            Text {
              textFormat: Text.PlainText
              text: Model.dayStatus(dayRow.day)
              color: dayRow.complete ? root.dim : root.foreground
              opacity: dayRow.complete ? 0.75 : 1.0
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }
        }

        PanelActionButton {
          visible: !dayRow.complete
          iconText: "󰇚"
          foreground: root.foreground
          fontFamily: root.fontFamily
          tooltipText: "Copy this day to disk"
          enabled: !gopro.syncing
          Layout.alignment: Qt.AlignVCenter
          onClicked: gopro.syncDay(dayRow.dayKey)
        }

        PanelActionButton {
          visible: dayRow.complete
          iconText: "󰉋"
          foreground: root.foreground
          fontFamily: root.fontFamily
          tooltipText: "Open this day's folder"
          Layout.alignment: Qt.AlignVCenter
          onClicked: gopro.openDay(dayRow.dayKey)
        }

        PanelActionButton {
          visible: dayRow.complete
          iconText: "󰩹"
          foreground: root.urgent
          fontFamily: root.fontFamily
          tooltipText: "Remove this day from the camera"
          enabled: !gopro.syncing
          Layout.alignment: Qt.AlignVCenter
          onClicked: root.askDeleteDay(dayRow.dayKey)
        }
      }

      // Thumbnails get their own line so the text above keeps the full width.
      // They come from the camera's own proxies and are cached on disk, so
      // reopening the panel costs nothing.
      Row {
        spacing: Style.space(4)
        visible: gopro.thumbnailsPerDay > 0 && children.length > 0
        Layout.fillWidth: true

        Repeater {
          model: Model.dayThumbs(gopro.files, dayRow.dayKey, gopro.thumbnailsPerDay)
          Rectangle {
            required property var modelData
            width: Style.space(34)
            height: Style.space(24)
            radius: Style.space(2)
            color: Qt.darker(root.foreground, 3.4)
            clip: true

            Image {
              id: thumbImage
              anchors.fill: parent
              source: gopro.thumbs[modelData.path] ? "file://" + gopro.thumbs[modelData.path] : ""
              fillMode: Image.PreserveAspectCrop
              asynchronous: true
              cache: true
              visible: status === Image.Ready
            }

            Text {
              anchors.centerIn: parent
              visible: !thumbImage.visible
              text: Model.fileGlyph(modelData.kind)
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }
        }
      }

      ProgressTrack {
        Layout.fillWidth: true
        visible: !dayRow.complete && Number(dayRow.day ? dayRow.day.syncedBytes : 0) > 0
        fraction: Model.dayProgress(dayRow.day)
      }
    }
  }

  component InfoPair: Row {
    property string label: ""
    property string value: ""
    property bool urgentValue: false

    width: parent.width
    spacing: Style.space(8)

    Text {
      textFormat: Text.PlainText
      text: parent.label
      color: root.foreground
      opacity: 0.6
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    Item {
      width: Math.max(0, parent.width - parent.children[0].implicitWidth
                         - parent.children[2].implicitWidth - parent.spacing * 2)
      height: 1
    }

    Text {
      textFormat: Text.PlainText
      text: parent.value
      color: parent.urgentValue ? root.urgent : root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      elide: Text.ElideRight
    }
  }
}
