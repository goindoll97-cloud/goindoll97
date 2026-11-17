"""Validate ON/OFF Qt switch behavior in a headless Windows CI session."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from desktop_app import LoadingDialog, ToggleSwitch, toggle_option


def main():
    app = QApplication.instance() or QApplication([])
    labels = (
        "누락 SMILES를 PubChem에서 자동 수집",
        "어려운 보류 사례만 로컬 AI 자동검토",
        "Excel 결과에 상세 감사자료 포함",
        "공식 법규 API 최신성 확인 (인터넷)",
    )
    for label in labels:
        widget = ToggleSwitch(label)
        assert widget.isCheckable()
        assert not widget.isChecked()
        assert widget.accessibleName() == label
        widget.show()
        app.processEvents()
        assert not widget.grab().isNull()

        # Both ends of the drawn track MUST respond to a real mouse click.
        # This tests the failure that a property-only smoke test missed.
        for pos, expected in (
            (QPoint(67, 17), True),
            (QPoint(9, 17), False),
            (QPoint(39, 17), True),
        ):
            QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=pos)
            assert widget.isChecked() is expected, (
                f"{label}: clicking the painted slider at {pos} failed"
            )
        widget.setChecked(True)
        assert widget.isChecked()
        assert not widget.grab().isNull()
        widget.setChecked(False)
        widget.setEnabled(False)
        assert not widget.isChecked()
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(67, 17))
        assert not widget.isChecked(), "Disabled switch must remain OFF"
        assert not widget.grab().isNull()
        row = toggle_option(label, widget)
        assert row.layout().count() == 2
        row.deleteLater()
    # A loading dialog can be dismissed without stopping its independent worker.
    dialog = LoadingDialog(None)
    assert dialog.title.text() == "로딩 중..."
    assert dialog.windowFlags() & Qt.WindowType.WindowCloseButtonHint
    assert dialog.windowModality() == Qt.WindowModality.ApplicationModal
    dialog.show()
    app.processEvents()
    QTest.mouseClick(dialog.close_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert not dialog.isVisible()

    dialog_x = LoadingDialog(None)
    dialog_x.show()
    app.processEvents()
    assert dialog_x.close()
    app.processEvents()
    assert not dialog_x.isVisible()
    print("PASS: ON/OFF mouse clicks and closable loading dialog")


if __name__ == "__main__":
    main()
