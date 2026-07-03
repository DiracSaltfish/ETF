APP_STYLESHEET = """
QWidget {
    background: #f4f1ea;
    color: #172033;
    font-family: "PingFang SC", "Hiragino Sans GB", sans-serif;
    font-size: 13px;
}
QMainWindow {
    background: #f4f1ea;
}
QFrame#heroCard, QFrame#panelCard, QFrame#metricCard, QFrame#noteCard {
    background: #fcfbf7;
    border: 1px solid #e0d8ca;
    border-radius: 18px;
}
QLabel#heroTitle {
    font-family: "Avenir Next", "PingFang SC", sans-serif;
    font-size: 28px;
    font-weight: 700;
    color: #10233d;
}
QLabel#heroSubtitle {
    color: #4f5c73;
    font-size: 13px;
}
QLabel#statusChip {
    background: #ddece4;
    color: #237a57;
    border-radius: 12px;
    padding: 6px 12px;
    font-weight: 600;
}
QLabel#sectionTitle {
    font-family: "Avenir Next", "PingFang SC", sans-serif;
    font-size: 16px;
    font-weight: 700;
    color: #10233d;
}
QLabel#mutedHint {
    color: #6b7484;
    font-size: 12px;
}
QLabel#metricTitle {
    color: #6b7484;
    font-size: 12px;
}
QLabel#metricValue {
    font-family: "Avenir Next", "PingFang SC", sans-serif;
    font-size: 24px;
    font-weight: 700;
    color: #10233d;
}
QLineEdit, QSpinBox, QComboBox, QAbstractSpinBox {
    background: #fffdfa;
    border: 1px solid #d4c8b4;
    border-radius: 12px;
    padding: 8px 10px;
    selection-background-color: #0f6c74;
}
QComboBox::drop-down {
    width: 28px;
    border: none;
}
QPushButton {
    background: #10233d;
    color: #fcfbf7;
    border: none;
    border-radius: 12px;
    padding: 10px 14px;
    font-weight: 600;
}
QPushButton:hover {
    background: #193457;
}
QPushButton:pressed {
    background: #0b1728;
}
QPushButton#secondaryButton {
    background: #e9e1d4;
    color: #10233d;
}
QPushButton#secondaryButton:hover {
    background: #ddd2bf;
}
QPushButton#dangerButton {
    background: #b84e34;
}
QPushButton#dangerButton:hover {
    background: #9f4129;
}
QPushButton:disabled {
    background: #cabfae;
    color: #8a8478;
}
QCheckBox {
    spacing: 8px;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
}
QTableWidget {
    background: #fffdfa;
    alternate-background-color: #f6f1e9;
    border: 1px solid #e0d8ca;
    border-radius: 14px;
    gridline-color: #eee7da;
    selection-background-color: #d7edf0;
    selection-color: #10233d;
}
QHeaderView::section {
    background: #efe6d8;
    color: #33425a;
    padding: 8px 10px;
    border: none;
    border-bottom: 1px solid #e0d8ca;
    font-weight: 700;
}
QTabWidget::pane {
    border: 1px solid #e0d8ca;
    border-radius: 16px;
    background: #fcfbf7;
    top: -1px;
}
QTabBar::tab {
    background: #e9e1d4;
    color: #526178;
    padding: 10px 16px;
    margin-right: 6px;
    border-top-left-radius: 12px;
    border-top-right-radius: 12px;
}
QTabBar::tab:selected {
    background: #fcfbf7;
    color: #10233d;
}
QPlainTextEdit {
    background: #fffdfa;
    border: 1px solid #e0d8ca;
    border-radius: 14px;
    padding: 8px;
    font-family: "Menlo", "Monaco", monospace;
}
QScrollBar:vertical {
    background: transparent;
    width: 12px;
    margin: 4px;
}
QScrollBar::handle:vertical {
    background: #c7b9a4;
    border-radius: 6px;
    min-height: 24px;
}
"""
