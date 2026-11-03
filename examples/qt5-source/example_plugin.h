#pragma once
#include <QObject>
#include <QtPlugin>

class IExample {
public:
    virtual ~IExample() = default;
    virtual void start() = 0;
};
Q_DECLARE_INTERFACE(IExample, "example.IExample/1.0")

// IIDを複数の実装が共有できる。プラグインの一意IDではない。
class ExamplePlugin : public QObject, public IExample {
    Q_OBJECT
    Q_PLUGIN_METADATA(IID "example.IExample/1.0" FILE "example_plugin.json")
    Q_INTERFACES(IExample)
    Q_PROPERTY(int count READ count NOTIFY countChanged)
public:
    int count() const { return 0; }
    Q_INVOKABLE void start() override { emit countChanged(count()); }
signals:
    void countChanged(int value);
public slots:
    void reset() { emit countChanged(0); }
};
