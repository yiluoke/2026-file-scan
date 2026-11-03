import QtQuick 2.12

Item {
    id: root
    property int currentCount: 0
    Connections {
        target: backend
        onCountChanged: root.currentCount = value
    }
}
