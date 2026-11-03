#include "example_plugin.h"
#include <QApplication>
#include <QPluginLoader>
#include <QQmlApplicationEngine>
#include <QQmlContext>

// 解析用。実際のプラグインDLLや生成uiヘッダーはこの例には含めない。
int main(int argc, char **argv) {
    QApplication app(argc, argv);
    ExamplePlugin backend;
    QObject::connect(&backend, &ExamplePlugin::countChanged,
                     &backend, [](int value) { Q_UNUSED(value); });
    QQmlApplicationEngine engine;
    engine.rootContext()->setContextProperty("backend", &backend);
    engine.load(QUrl("qrc:/screens/Main.qml"));
    QPluginLoader plugin("plugins/example");
    return app.exec();
}
