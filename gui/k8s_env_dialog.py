# -*- coding: utf-8 -*-
"""K8s 环境管理弹窗（添加 / 切换 / 删除多环境 kubeconfig）。"""
import re
from pathlib import Path

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLineEdit, QPushButton, QHBoxLayout,
    QPlainTextEdit, QLabel, QListWidget, QDialogButtonBox,
    QFileDialog,
)
from core import k8s_manager as km


class EnvManageDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("环境管理")
        self.resize(560, 420)
        lay = QVBoxLayout(self)

        form = QFormLayout()
        self.name = QLineEdit(); self.name.setPlaceholderText("英文标识，如 dev")
        self.label = QLineEdit(); self.label.setPlaceholderText("显示名，如 开发")
        self.kubeconfig = QLineEdit(); self.kubeconfig.setPlaceholderText("kubeconfig 文件路径")
        self.btn_kc = QPushButton("浏览…")
        self.btn_kc.clicked.connect(self._browse)
        kc_row = QHBoxLayout(); kc_row.addWidget(self.kubeconfig, 1); kc_row.addWidget(self.btn_kc)
        self.context = QLineEdit(); self.context.setPlaceholderText("可选，留空用当前上下文")
        self.namespace = QLineEdit("default")
        self.intranet = QPlainTextEdit(); self.intranet.setPlaceholderText("每行一个 host:port，如 10.6.6.254:8080")
        self.intranet.setMaximumHeight(70)
        form.addRow("环境标识", self.name)
        form.addRow("显示名", self.label)
        form.addRow("kubeconfig", kc_row)
        form.addRow("context", self.context)
        form.addRow("默认命名空间", self.namespace)
        form.addRow("内网探测主机", self.intranet)
        lay.addLayout(form)

        btn = QHBoxLayout()
        self.btn_save = QPushButton("保存"); self.btn_save.setObjectName("primary")
        self.btn_switch = QPushButton("切换为当前")
        self.btn_delete = QPushButton("删除")
        btn.addWidget(self.btn_save); btn.addWidget(self.btn_switch); btn.addWidget(self.btn_delete)
        btn.addStretch(1)
        self.msg = QLabel("")
        lay.addLayout(btn)
        lay.addWidget(self.msg)

        self.listw = QListWidget()
        self.listw.itemClicked.connect(self._on_pick)
        lay.addWidget(QLabel("已配置环境（点击载入）："))
        lay.addWidget(self.listw, 1)

        dlg = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        dlg.rejected.connect(self.reject)
        lay.addWidget(dlg)

        self.btn_save.clicked.connect(self._save)
        self.btn_switch.clicked.connect(self._switch)
        self.btn_delete.clicked.connect(self._delete)
        self._refresh()

    def _browse(self):
        p, _ = QFileDialog.getOpenFileName(self, "选择 kubeconfig", str(Path.home()), "YAML (*.yaml *.yml);;All (*)")
        if p:
            self.kubeconfig.setText(p)

    def _refresh(self):
        self.listw.clear()
        for name, label, is_cur in km.list_envs():
            env = km.get_env(name)[1]
            txt = "%s (%s)%s  kubeconfig: %s" % (
                label, name, "  [当前]" if is_cur else "",
                env.get("kubeconfig") or "(无)")
            self.listw.addItem(txt)
        self.msg.setText("")

    def _on_pick(self, item):
        # 从列表文本解析 name（格式 "显示名 (name) ..."）
        m = re.search(r"\(([^)]+)\)", item.text())
        if not m:
            return
        name = m.group(1)
        _, env = km.get_env(name)
        self.name.setText(name)
        self.label.setText(env.get("label", name))
        self.kubeconfig.setText(env.get("kubeconfig", ""))
        self.context.setText(env.get("context", ""))
        self.namespace.setText(env.get("namespace", "default"))
        self.intranet.setPlainText("\n".join(env.get("intranet_hosts", [])))

    def _save(self):
        name = self.name.text().strip()
        if not name:
            self.msg.setText("请填写环境标识"); return
        km.add_or_update_env(
            name,
            label=self.label.text().strip(),
            kubeconfig=self.kubeconfig.text().strip(),
            context=self.context.text().strip(),
            namespace=self.namespace.text().strip() or "default",
            intranet_hosts=[s.strip() for s in self.intranet.toPlainText().splitlines() if s.strip()],
        )
        self.msg.setText("已保存：%s" % name)
        self._refresh()

    def _switch(self):
        name = self.name.text().strip()
        if not name:
            self.msg.setText("请先填写/选择环境"); return
        km.set_current_env(name)
        self.msg.setText("已切换为：%s" % name)
        self._refresh()

    def _delete(self):
        name = self.name.text().strip()
        if not name:
            return
        km.delete_env(name)
        self.msg.setText("已删除：%s" % name)
        self._refresh()
