"""Validate ON/OFF Qt switch behavior in a headless Windows CI session."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
import pandas as pd
from PySide6.QtWidgets import QApplication, QTabWidget, QTableView
from desktop_app import DesktopWindow, LoadingDialog, ToggleSwitch, toggle_option


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
    # The live desktop UI has one result table, without any extra detail tabs.
    # Avoid starting background catalog initialization during this UI-only test.
    original_load = DesktopWindow.load_catalog
    DesktopWindow.load_catalog = lambda self: None
    try:
        window = DesktopWindow()
        assert not window.findChildren(QTabWidget), "Only one result table should be visible"
        assert window.findChildren(QTableView) == [window.result_table]
        assert window.result_table.objectName() == "judgementResults"
        row = pd.DataFrame([{
            "물질명": "시험물질", "판정결과": "승인 규칙상 해당",
            "규제 카테고리": "인체급성유해성물질",
            "판단근거": "직접 CAS 일치", "법적 근거": "고유번호 예시",
        }])
        window.catalog = {"ai_status": {"ready": False}}
        window.result = {
            "plain": row, "summary": row,
            "saved": {"file_name": "test.xlsx"},
            "llm_cases": [], "llm_audit": pd.DataFrame(),
        }
        window._show_result()
        model = window.result_table.model()
        assert model.rowCount() == 1
        assert model.columnCount() == 5
        assert model.frame.iloc[0]["법적 근거"] == "고유번호 예시"
        window.close()
    finally:
        DesktopWindow.load_catalog = original_load
    print("PASS: ON/OFF, closable loading dialog, single judgement results table")


if __name__ == "__main__":
    main()
