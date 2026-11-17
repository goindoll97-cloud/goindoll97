"""Validate ON/OFF Qt switch behavior in a headless Windows CI session."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from desktop_app import ToggleSwitch, toggle_option


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
        assert not widget.grab().isNull()
        widget.setChecked(True)
        assert widget.isChecked()
        assert not widget.grab().isNull()
        widget.setChecked(False)
        widget.setEnabled(False)
        assert not widget.isChecked()
        assert not widget.grab().isNull()
        row = toggle_option(label, widget)
        assert row.layout().count() == 2
        row.deleteLater()
    print("PASS: native desktop ON/OFF switch render, state and disabled mode")


if __name__ == "__main__":
    main()
