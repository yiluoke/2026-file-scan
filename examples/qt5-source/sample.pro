QT += core widgets qml
SOURCES += main.cpp
HEADERS += example_plugin.h
FORMS += main_window.ui
RESOURCES += screens.qrc
# 初期版はコマンドを実行しない。
QMAKE_EXTRA_TARGETS += custom_help
custom_help.target = show-help
custom_help.commands = echo "Example target"
