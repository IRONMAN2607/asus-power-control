#!/usr/bin/python3
"""Armoury-style telemetry OSD for ASUS Power Control."""

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QVBoxLayout, QWidget

from monitoring import Metrics, TelemetryCollector


def value(number, suffix="", decimals=0):
    return "N/A" if number is None else f"{number:.{decimals}f}{suffix}"


def ram_value(used, total):
    if used is None or total is None:
        return "N/A"
    return f"{used / 1024**3:.1f}/{total / 1024**3:.1f} GiB"


def vram_value(used, total):
    if used is None or total is None:
        return "N/A"
    return f"{used}/{total} MiB"


class MonitorOSD(QWidget):
    DEFAULT_FILTERS = {
        "cpu_basic": True,
        "cpu_power": True,
        "memory": True,
        "gpu_basic": True,
        "gpu_memory": True,
        "gpu_power": True,
        "fps": True,
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.collector = TelemetryCollector()
        self.setWindowTitle("ASUS System Monitor")
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setMinimumWidth(330)
        self.panel = QFrame(self)
        self.panel.setObjectName("panel")
        self.panel.setStyleSheet("""
            QFrame#panel {
                background: rgba(12, 16, 22, 158);
                border: 1px solid rgba(120, 170, 255, 92);
                border-radius: 14px;
            }
            QLabel { color: #f2f5fb; }
            QLabel#title { color: #8db8ff; font-size: 16px; font-weight: 700; }
            QLabel#subtitle { color: #aeb9ca; font-size: 10px; }
            QLabel#section { color: #79aaff; font-size: 11px; font-weight: 700; }
            QLabel#name { color: #aeb9ca; }
            QLabel#metric { color: #ffffff; font-size: 13px; font-weight: 600; }
        """)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        panel_layout = QVBoxLayout(self.panel)
        panel_layout.setContentsMargins(16, 14, 16, 14)
        panel_layout.setSpacing(5)
        title = QLabel("ASUS SYSTEM MONITOR")
        title.setObjectName("title")
        panel_layout.addWidget(title)
        subtitle = QLabel("Realtime telemetry  •  Ctrl+Alt+S to hide")
        subtitle.setObjectName("subtitle")
        panel_layout.addWidget(subtitle)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(22)
        self.grid.setVerticalSpacing(5)
        panel_layout.addLayout(self.grid)
        self.rows = {}
        self.sections = {}
        self.row_groups = {}
        self._section("CPU")
        self._row("Temperature", "cpu_temperature", "cpu_basic")
        self._row("Usage", "cpu_usage", "cpu_basic")
        self._row("Power draw", "cpu_power", "cpu_power")
        self._row("Voltage", "cpu_voltage", "cpu_power")
        self._section("MEMORY")
        self._row("RAM", "memory", "memory")
        self._section("GPU")
        self._row("State", "gpu_state", "gpu_basic")
        self._row("Temperature", "gpu_temperature", "gpu_basic")
        self._row("Usage", "gpu_usage", "gpu_basic")
        self._row("VRAM", "vram", "gpu_memory")
        self._row("Power draw", "gpu_power", "gpu_power")
        self._row("Voltage", "gpu_voltage", "gpu_power")
        self._row("FPS", "fps", "fps")
        self.note = QLabel("Waiting for telemetry…")
        self.note.setObjectName("subtitle")
        self.note.setWordWrap(True)
        panel_layout.addWidget(self.note)
        outer.addWidget(self.panel)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.update_metrics)

    def _section(self, text):
        row = self.grid.rowCount()
        label = QLabel(text)
        label.setObjectName("section")
        self.grid.addWidget(label, row, 0, 1, 2)
        self.sections[text] = label

    def _row(self, name, key, group):
        row = self.grid.rowCount()
        name_label = QLabel(name)
        name_label.setObjectName("name")
        metric = QLabel("N/A")
        metric.setObjectName("metric")
        metric.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.grid.addWidget(name_label, row, 0)
        self.grid.addWidget(metric, row, 1)
        self.rows[key] = metric
        self.row_groups[key] = group

    def set_filters(self, filters):
        filters = {**self.DEFAULT_FILTERS, **(filters or {})}
        for key, metric in self.rows.items():
            visible = bool(filters.get(self.row_groups[key], True))
            metric.setVisible(visible)
            index = self.grid.indexOf(metric)
            row, _column, _row_span, _column_span = self.grid.getItemPosition(index)
            label = self.grid.itemAtPosition(row, 0)
            if label is not None and label.widget() is not None:
                label.widget().setVisible(visible)
        for section_name, section in self.sections.items():
            section_groups = {
                "CPU": {"cpu_basic", "cpu_power"},
                "MEMORY": {"memory"},
                "GPU": {"gpu_basic", "gpu_memory", "gpu_power", "fps"},
            }[section_name]
            section.setVisible(any(filters.get(group, True) for group in section_groups))
        self.adjustSize()
        if self.isVisible():
            self._position()

    def _position(self):
        screen = self.screen()
        if screen is None and self.windowHandle() is not None:
            screen = self.windowHandle().screen()
        if screen is None:
            from PySide6.QtWidgets import QApplication
            screen = QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.adjustSize()
        self.move(area.left() + 24, area.top() + 24)

    def toggle(self):
        if self.isVisible():
            self.hide()
        else:
            self._position()
            self.show()
            self.raise_()
            self.update_metrics()
            self.timer.start()

    def showEvent(self, event):
        self._position()
        self.timer.start()
        self.update_metrics()
        super().showEvent(event)

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def update_metrics(self):
        try:
            metrics = self.collector.sample()
        except Exception as error:  # Telemetry must never take down the OSD.
            self.note.setText(f"Telemetry error: {error}")
            return
        self.display(metrics)

    def display(self, metrics: Metrics):
        self.rows["cpu_temperature"].setText(value(metrics.cpu_temperature, " °C", 0))
        self.rows["cpu_usage"].setText(value(metrics.cpu_usage, "%", 0))
        self.rows["cpu_power"].setText(value(metrics.cpu_power, " W", 1))
        self.rows["cpu_voltage"].setText(value(metrics.cpu_voltage, " V", 3))
        self.rows["memory"].setText(f"{value(metrics.memory_percent, '%', 0)}  " +
                                     ram_value(metrics.memory_used, metrics.memory_total))
        self.rows["gpu_state"].setText(metrics.gpu_state)
        self.rows["gpu_temperature"].setText(value(metrics.gpu_temperature, " °C", 0))
        self.rows["gpu_usage"].setText(value(metrics.gpu_usage, "%", 0))
        self.rows["vram"].setText(value(metrics.gpu_memory_usage, "%", 0) + "  " +
                                   vram_value(metrics.vram_used, metrics.vram_total))
        self.rows["gpu_power"].setText(value(metrics.gpu_power, " W", 1))
        self.rows["gpu_voltage"].setText(value(metrics.gpu_voltage, " V", 3))
        self.rows["fps"].setText(value(metrics.fps, " FPS", 1))
        notes = [note for note in (metrics.gpu_note, metrics.fps_note) if note]
        self.note.setText("  •  ".join(notes))
