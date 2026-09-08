import QtQuick
import qs.Commons

// The camera's silhouette: squarish body, big lens, and the small lug on the
// top corner. Drawn rather than borrowed from a font so it stays legible at
// bar size and inherits the theme's foreground like every other bar glyph.
Item {
  id: root

  property real iconSize: Style.font.icon
  property color color: Color.foreground
  property real stroke: Math.max(1, Math.round(iconSize * 0.09))

  width: iconSize * 1.1
  height: iconSize
  implicitWidth: iconSize * 1.1
  implicitHeight: iconSize

  Rectangle {
    id: body
    anchors.fill: parent
    radius: Math.round(root.height * 0.26)
    color: "transparent"
    border.color: root.color
    border.width: root.stroke
    antialiasing: true
  }

  // Lens barrel.
  Rectangle {
    anchors.centerIn: body
    width: Math.round(root.height * 0.46)
    height: width
    radius: width / 2
    color: "transparent"
    border.color: root.color
    border.width: root.stroke
    antialiasing: true
  }

  // Lens centre, so the shape still reads as a camera when very small.
  Rectangle {
    anchors.centerIn: body
    width: Math.max(1, Math.round(root.height * 0.14))
    height: width
    radius: width / 2
    color: root.color
    antialiasing: true
  }

  // Mode button on the left edge.
  Rectangle {
    anchors.verticalCenter: body.verticalCenter
    anchors.right: body.left
    anchors.rightMargin: -root.stroke / 2
    width: Math.max(1, Math.round(root.height * 0.09))
    height: Math.round(root.height * 0.22)
    radius: width / 2
    color: root.color
    antialiasing: true
  }
}
