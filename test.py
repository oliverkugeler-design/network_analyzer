import sys
from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLineEdit, QLabel

class HalloApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Hallo-Generator")

        self.input = QLineEdit()
        self.input.setPlaceholderText("Gib deinen Namen ein …")

        self.output = QLabel("hallo")
        self.output.setStyleSheet("font-size: 16px;")

        layout = QVBoxLayout()
        layout.addWidget(self.input)
        layout.addWidget(self.output)
        self.setLayout(layout)

        # Aktualisiere die Ausgabe bei jeder Eingabe
        self.input.textChanged.connect(self.update_output)

    def update_output(self, text: str):
        text = text.strip()
        if text:
            self.output.setText(f"hallo {text}")
        else:
            self.output.setText("hallo")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = HalloApp()
    w.resize(320, 120)
    w.show()
    sys.exit(app.exec())
