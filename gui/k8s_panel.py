# -*- coding: utf-8 -*-
"""K8s 多环境运维面板（PyQt6）。

在「快照」能力之上，整合：
- 多环境管理（开发 / 测试 / 正式，各自 kubeconfig / context / namespace / 内网探测主机）
- Pod / 资源 YAML 获取 → 编辑 → 上传(apply)
- 网络检测（kubectl / kubeconfig / 集群连通 / 内网 API Server / 外网）

职责（纯视图 + 配置收集 + 信号）：
- 发出 snapshotRequested / cancelRequested / yamlGetRequested / yamlApplyRequested / netRequested
- 接收主窗口回调填充：set_running / append_log / set_progress / set_result / set_error，
  以及 set_yaml / set_yaml_result / set_net_result
- 配置（抓取参数 + 当前环境）通过 QSettings 持久化

后台执行统一交给 MainWindow 的 Worker（core.k8s_snapshot.run_snapshot / core.k8s_manager 函数），
本文件不自行开线程，避免 UI 线程安全问题。
"""
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal, QSettings, QUrl
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QTabWidget,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from core import k8s_manager as km

_SEV_FG = {"HIGH": "#c0392b", "MED": "#d97706", "OK": None}
_COLUMNS = ["名称", "状态", "就绪", "重启", "原因", "节点", "HostIP", "PodIP", "时长", "严重度"]


# --------------------------------------------------------------------- 后台任务包装
# Worker 按参数名注入 on_log/on_progress/should_cancel；这些包装让 core.k8s_manager
# 的函数能以统一方式在子线程跑，并把日志信号接到 UI。
def yaml_get_task(env, kind, name, namespace, clean=True):
    return km.get_resource_yaml(env, kind, name, namespace or None, clean=clean)


def yaml_apply_task(env, kind, name, namespace, content):
    out, err = km.apply_yaml_content(env, content, namespace or None)
    return {"stdout": out, "stderr": err}


def net_task(env, extra_hosts, on_log=None):
    return km.detect_network(env, extra_hosts or None, on_log=on_log)


# --------------------------------------------------------------------- 环境管理弹窗
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
        import re
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


# --------------------------------------------------------------------- 主面板
class K8sPanel(QWidget):
    snapshotRequested = pyqtSignal(dict)
    cancelRequested = pyqtSignal()
    yamlGetRequested = pyqtSignal(dict)
    yamlApplyRequested = pyqtSignal(dict)
    netRequested = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._settings = QSettings("jira-git-gui", "JiraGitGUI")
        self._out_dir = None
        self._report = None
        self._records = []
        self._env = None
        self._running = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        # -------------------------------------------------- 环境栏
        env_bar = QHBoxLayout()
        env_bar.addWidget(QLabel("环境"))
        self.env_combo = QComboBox()
        self.env_combo.setMinimumWidth(160)
        self.env_combo.currentTextChanged.connect(self._on_env_changed)
        env_bar.addWidget(self.env_combo, 1)
        self.btn_env_manage = QPushButton("管理环境")
        self.btn_env_manage.clicked.connect(self._open_env_manage)
        env_bar.addWidget(self.btn_env_manage)
        self.env_kc = QLabel("")
        self.env_kc.setStyleSheet("color:#6b7280;font-size:12px;")
        env_bar.addWidget(self.env_kc)
        layout.addLayout(env_bar)

        # -------------------------------------------------- 子标签
        self.sub = QTabWidget()
        self.sub.addTab(self._build_snapshot_tab(), "📸 快照")
        self.sub.addTab(self._build_yaml_tab(), "📝 Pod YAML")
        self.sub.addTab(self._build_net_tab(), "🌐 网络检测")
        layout.addWidget(self.sub, 1)

        self._refresh_envs()
        self._restore_settings()

    # ================================================== 快照子页
    def _build_snapshot_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        cfg = QGroupBox("抓取配置")
        cl = QVBoxLayout(cfg)
        cl.setSpacing(8)

        r1 = QHBoxLayout(); r1.setSpacing(8)
        r1.addWidget(QLabel("命名空间"))
        self.namespace = QLineEdit(); self.namespace.setPlaceholderText("默认（当前环境）")
        r1.addWidget(self.namespace, 2)
        r1.addWidget(QLabel("Label 选择器"))
        self.selector = QLineEdit(); self.selector.setPlaceholderText("如 app=hcm-core")
        r1.addWidget(self.selector, 2)
        r1.addWidget(QLabel("Pod 名正则"))
        self.pod_filter = QLineEdit(); self.pod_filter.setPlaceholderText("如 hcm-core|celery")
        r1.addWidget(self.pod_filter, 2)
        cl.addLayout(r1)

        r2 = QHBoxLayout(); r2.setSpacing(8)
        r2.addWidget(QLabel("日志行数"))
        self.tail = QSpinBox(); self.tail.setRange(10, 5000); self.tail.setValue(200)
        self.tail.setSuffix(" 行"); r2.addWidget(self.tail)
        r2.addWidget(QLabel("重启阈值"))
        self.restart_th = QSpinBox(); self.restart_th.setRange(0, 100); self.restart_th.setValue(5)
        self.restart_th.setToolTip("重启次数 ≥ 此值视为异常(HIGH)")
        self.restart_th.setSuffix(" 次"); r2.addWidget(self.restart_th)
        r2.addWidget(QLabel("输出目录"))
        self.out_dir = QLineEdit(); self.out_dir.setPlaceholderText("默认 ~/k8s_snapshots/<时间戳>")
        r2.addWidget(self.out_dir, 3)
        self.btn_browse = QPushButton("浏览…"); self.btn_browse.clicked.connect(self._browse_dir)
        r2.addWidget(self.btn_browse)
        cl.addLayout(r2)

        r3 = QHBoxLayout(); r3.setSpacing(8)
        self.all_logs = QCheckBox("全量抓日志（默认仅异常 Pod）")
        self.prev_logs = QCheckBox("含重启前日志(--previous)")
        self.kubeconfig = QLineEdit(); self.kubeconfig.setPlaceholderText("留空则用当前环境的 kubeconfig")
        r3.addWidget(self.all_logs); r3.addWidget(self.prev_logs); r3.addWidget(self.kubeconfig, 3)
        cl.addLayout(r3)

        r4 = QHBoxLayout(); r4.setSpacing(8)
        self.btn_run = QPushButton("抓取快照"); self.btn_run.setObjectName("primary")
        self.btn_run.clicked.connect(self._on_run)
        self.btn_cancel = QPushButton("取消"); self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(lambda: self.cancelRequested.emit())
        self.btn_open_report = QPushButton("打开报告"); self.btn_open_report.setEnabled(False)
        self.btn_open_report.clicked.connect(self._open_report)
        self.btn_open_dir = QPushButton("打开目录"); self.btn_open_dir.setEnabled(False)
        self.btn_open_dir.clicked.connect(self._open_dir)
        r4.addWidget(self.btn_run); r4.addWidget(self.btn_cancel)
        r4.addWidget(self.btn_open_report); r4.addWidget(self.btn_open_dir); r4.addStretch(1)
        self.progress_label = QLabel(""); self.progress_label.setStyleSheet("color:#6b7280;font-size:12px;")
        r4.addWidget(self.progress_label)
        cl.addLayout(r4)
        v.addWidget(cfg)

        self.summary = QLabel("尚未抓取。配置后点击「抓取快照」。")
        self.summary.setStyleSheet("color:#6b7280;font-size:12px;padding:2px 4px;")
        v.addWidget(self.summary)

        split = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.cellClicked.connect(self._on_cell_clicked)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, hdr.ResizeMode.Stretch)
        for i in range(1, len(_COLUMNS)):
            hdr.setSectionResizeMode(i, hdr.ResizeMode.ResizeToContents)
        split.addWidget(self.table)

        bottom = QWidget(); bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0); bl.setSpacing(4)
        bl.addWidget(QLabel("Pod 日志（点击上方某行查看）："))
        self.log_view = QPlainTextEdit(); self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet("font-family: Menlo, Monaco, 'Courier New', monospace; font-size: 12px;")
        bl.addWidget(self.log_view, 1)
        split.addWidget(bottom)
        split.setStretchFactor(0, 3); split.setStretchFactor(1, 2)
        v.addWidget(split, 1)
        return w

    # ================================================== Pod YAML 子页
    def _build_yaml_tab(self):
        w = QWidget(); v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0); v.setSpacing(10)

        cfg = QGroupBox("获取 / 上传")
        cl = QHBoxLayout(cfg); cl.setSpacing(8)
        cl.addWidget(QLabel("类型"))
        self.yaml_kind = QComboBox()
        self.yaml_kind.addItems(["pod", "deployment", "service", "configmap", "ingress", "statefulset"])
        cl.addWidget(self.yaml_kind)
        cl.addWidget(QLabel("名称"))
        self.yaml_name = QLineEdit(); self.yaml_name.setPlaceholderText("如 hcm-core-6bc569958d-2ggkx")
        cl.addWidget(self.yaml_name, 2)
        cl.addWidget(QLabel("命名空间"))
        self.yaml_ns = QLineEdit(); self.yaml_ns.setPlaceholderText("留空用环境默认")
        cl.addWidget(self.yaml_ns, 2)
        self.btn_yaml_get = QPushButton("获取 YAML"); self.btn_yaml_get.clicked.connect(self._on_yaml_get)
        self.btn_yaml_apply = QPushButton("修改后上传"); self.btn_yaml_apply.setObjectName("primary")
        self.btn_yaml_apply.clicked.connect(self._on_yaml_apply)
        cl.addWidget(self.btn_yaml_get); cl.addWidget(self.btn_yaml_apply)
        self.yaml_clean = QCheckBox("清洗输出(移除 status/服务端字段)")
        self.yaml_clean.setChecked(True)
        cl.addWidget(self.yaml_clean)
        self.yaml_msg = QLabel(""); self.yaml_msg.setStyleSheet("color:#6b7280;font-size:12px;")
        cl.addWidget(self.yaml_msg)
        v.addWidget(cfg)

        v.addWidget(QLabel("YAML 内容（编辑后上传）："))
        self.yaml_edit = QPlainTextEdit()
        self.yaml_edit.setStyleSheet("font-family: Menlo, Monaco, 'Courier New', monospace; font-size: 12px;")
        self.yaml_edit.setPlainText("# 点击「获取 YAML」从集群拉取，编辑后点「修改后上传」")
        v.addWidget(self.yaml_edit, 1)
        self.yaml_out = QTextEdit(); self.yaml_out.setReadOnly(True)
        self.yaml_out.setMaximumHeight(140)
        self.yaml_out.setStyleSheet("font-family: Menlo, Monaco, monospace; font-size: 12px; background:#0f172a; color:#e2e8f0;")
        self.yaml_out.setVisible(False)
        v.addWidget(self.yaml_out)
        return w

    # ================================================== 网络检测子页
    def _build_net_tab(self):
        w = QWidget(); v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0); v.setSpacing(10)

        cfg = QGroupBox("网络检测（含内网环境）")
        cl = QVBoxLayout(cfg); cl.setSpacing(8)
        cl.addWidget(QLabel("额外内网主机（每行一个 host:port，可选）："))
        self.net_hosts = QPlainTextEdit("10.6.6.254:8080\n192.168.10.1:80")
        self.net_hosts.setMaximumHeight(70)
        self.net_hosts.setStyleSheet("font-family: Menlo, Monaco, monospace; font-size: 12px;")
        cl.addWidget(self.net_hosts)
        r = QHBoxLayout(); r.setSpacing(8)
        self.btn_net = QPushButton("开始检测"); self.btn_net.setObjectName("primary")
        self.btn_net.clicked.connect(self._on_net)
        self.net_summary = QLabel(""); self.net_summary.setStyleSheet("color:#6b7280;font-size:12px;")
        r.addWidget(self.btn_net); r.addWidget(self.net_summary); r.addStretch(1)
        cl.addLayout(r)
        v.addWidget(cfg)

        v.addWidget(QLabel("检测结果："))
        self.net_list = QListWidget(); v.addWidget(self.net_list, 1)

        v.addWidget(QLabel("内网探测："))
        self.net_intranet = QListWidget(); v.addWidget(self.net_intranet, 1)

        self.net_verdict = QLabel(""); self.net_verdict.setStyleSheet("color:#374151;font-size:12px;padding:4px;")
        v.addWidget(self.net_verdict)
        return w

    # ================================================== 环境栏逻辑
    def _refresh_envs(self):
        self.env_combo.blockSignals(True)
        self.env_combo.clear()
        cur = None
        for name, label, is_cur in km.list_envs():
            self.env_combo.addItem("%s (%s)" % (label, name), name)
            if is_cur:
                cur = name
        saved = self._settings.value("k8s/env", "")
        if saved and self.env_combo.findData(saved) >= 0:
            self.env_combo.setCurrentIndex(self.env_combo.findData(saved))
            self._env = saved
        elif cur:
            self.env_combo.setCurrentIndex(self.env_combo.findData(cur))
            self._env = cur
        self.env_combo.blockSignals(False)
        self._update_env_kc()

    def _update_env_kc(self):
        if not self._env:
            return
        try:
            _, env = km.get_env(self._env)
            kc = env.get("kubeconfig")
            self.env_kc.setText("kubeconfig: " + (kc or "(未配置)"))
        except Exception:
            self.env_kc.setText("")

    def _on_env_changed(self, _text):
        self._env = self.env_combo.currentData()
        self._settings.setValue("k8s/env", self._env or "")
        self._update_env_kc()

    def _open_env_manage(self):
        dlg = EnvManageDialog(self)
        dlg.exec()
        self._refresh_envs()

    # ================================================== 配置持久化
    def _restore_settings(self):
        s = self._settings
        self.namespace.setText(s.value("k8s/namespace", ""))
        self.selector.setText(s.value("k8s/selector", ""))
        self.pod_filter.setText(s.value("k8s/pod_filter", ""))
        self.tail.setValue(int(s.value("k8s/tail", 200)))
        self.restart_th.setValue(int(s.value("k8s/restart_threshold", 5)))
        self.out_dir.setText(s.value("k8s/out_dir", ""))
        self.kubeconfig.setText(s.value("k8s/kubeconfig", ""))
        self.all_logs.setChecked(s.value("k8s/all_logs", "false") == "true")
        self.prev_logs.setChecked(s.value("k8s/include_previous", "false") == "true")

    def _save_settings(self):
        s = self._settings
        s.setValue("k8s/namespace", self.namespace.text().strip())
        s.setValue("k8s/selector", self.selector.text().strip())
        s.setValue("k8s/pod_filter", self.pod_filter.text().strip())
        s.setValue("k8s/tail", self.tail.value())
        s.setValue("k8s/restart_threshold", self.restart_th.value())
        s.setValue("k8s/out_dir", self.out_dir.text().strip())
        s.setValue("k8s/kubeconfig", self.kubeconfig.text().strip())
        s.setValue("k8s/all_logs", "true" if self.all_logs.isChecked() else "false")
        s.setValue("k8s/include_previous", "true" if self.prev_logs.isChecked() else "false")

    # ================================================== 对外回调（主线程）
    def append_log(self, msg: str) -> None:
        self.log_view.appendPlainText(msg)

    def set_progress(self, done: int, total: int, name: str) -> None:
        if total and total > 0:
            self.progress_label.setText(f"已处理 {done}/{total}（{name}）")
        else:
            self.progress_label.setText(f"已处理 {done} …")

    def set_running(self, on: bool) -> None:
        self._running = on
        self.btn_run.setEnabled(not on)
        self.btn_run.setText("抓取中…" if on else "抓取快照")
        self.btn_cancel.setEnabled(on)

    def set_error(self, msg: str) -> None:
        self.append_log("✗ 出错：" + msg)
        self.summary.setText("抓取失败，详见日志。")

    def set_result(self, result: dict) -> None:
        self._out_dir = result.get("out_dir")
        self._report = result.get("report")
        self._records = result.get("records", [])
        sm = result.get("summary", {})
        cancelled = sm.get("cancelled")
        self.summary.setText(
            f"共 {sm.get('total', 0)} | 正常 {sm.get('ok', 0)} | "
            f"警告 {sm.get('med', 0)} | 异常 {sm.get('high', 0)} | "
            f"抓取日志 {sm.get('logs', 0)}"
            + ("（已取消）" if cancelled else ""))
        self._fill_table(self._records)
        self.btn_open_report.setEnabled(bool(self._report))
        self.btn_open_dir.setEnabled(bool(self._out_dir))

    # YAML 回调
    def set_yaml(self, yaml_text: str) -> None:
        self.yaml_edit.setPlainText(yaml_text)
        self.yaml_out.setVisible(False)
        self.yaml_msg.setText("已获取 YAML")

    def set_yaml_result(self, res: dict) -> None:
        self.yaml_out.setVisible(True)
        self.yaml_out.setPlainText((res.get("stdout") or "") + ("\n" + res["stderr"] if res.get("stderr") else ""))
        self.yaml_msg.setText("✅ 上传成功")

    # 网络检测回调
    def set_net_result(self, res: dict) -> None:
        self.net_summary.setText(res.get("summary", ""))
        self.net_list.clear()
        for c in res.get("checks", []):
            icon = "✓" if c["status"] == "ok" else ("✕" if c["status"] == "fail" else "!")
            color = {"ok": "#16a34a", "fail": "#c0392b", "warn": "#d97706"}[c["status"]]
            item = QListWidgetItem(f"{icon}  {c['name']}\n    {c['detail']}")
            item.setForeground(QColor(color))
            self.net_list.addItem(item)
        self.net_intranet.clear()
        for r in res.get("intranet", []):
            icon = "✓" if r["ok"] else "✕"
            color = "#16a34a" if r["ok"] else "#c0392b"
            item = QListWidgetItem(f"{icon}  {r['target']}  " + (f"(延迟 {r['ms']}ms)" if r["ok"] else "不可达"))
            item.setForeground(QColor(color))
            self.net_intranet.addItem(item)
        verdict = ("判定：当前可连接该环境集群与内网，可正常运维。" if res.get("cluster_ok")
                   else "判定：未连通集群（可能未接入对应内网/VPN 或 kubeconfig 缺失）。请确认后重试。")
        self.net_verdict.setText(verdict)

    def set_busy(self, on: bool, which: str) -> None:
        if which == "yaml":
            self.btn_yaml_get.setEnabled(not on); self.btn_yaml_apply.setEnabled(not on)
            self.yaml_msg.setText("处理中…" if on else self.yaml_msg.text())
        elif which == "net":
            self.btn_net.setEnabled(not on)
            self.net_summary.setText("检测中…" if on else "")

    # ================================================== 内部
    def _collect_opts(self) -> dict:
        self._save_settings()
        opts = {
            "namespace": self.namespace.text().strip() or None,
            "selector": self.selector.text().strip() or None,
            "pod_filter": self.pod_filter.text().strip() or None,
            "tail": self.tail.value(),
            "restart_threshold": self.restart_th.value(),
            "all_logs": self.all_logs.isChecked(),
            "include_previous": self.prev_logs.isChecked(),
            "kubeconfig": self.kubeconfig.text().strip() or None,
            "env": self._env,
        }
        od = self.out_dir.text().strip()
        if od:
            opts["out_dir"] = od
        return opts

    def _on_run(self) -> None:
        self.snapshotRequested.emit(self._collect_opts())

    def _on_yaml_get(self) -> None:
        name = self.yaml_name.text().strip()
        if not name:
            self.yaml_msg.setText("请填写资源名称"); return
        self.yamlGetRequested.emit({
            "env": self._env,
            "kind": self.yaml_kind.currentText(),
            "name": name,
            "namespace": self.yaml_ns.text().strip() or None,
            "clean": self.yaml_clean.isChecked(),
        })

    def _on_yaml_apply(self) -> None:
        content = self.yaml_edit.toPlainText()
        if not content.strip():
            self.yaml_msg.setText("内容为空"); return
        self.yamlApplyRequested.emit({
            "env": self._env,
            "kind": self.yaml_kind.currentText(),
            "name": self.yaml_name.text().strip(),
            "namespace": self.yaml_ns.text().strip() or None,
            "content": content,
        })

    def _on_net(self) -> None:
        hosts = [s.strip() for s in self.net_hosts.toPlainText().splitlines() if s.strip()]
        self.netRequested.emit({"env": self._env, "extra_hosts": hosts})

    def _browse_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择输出目录", str(Path.home()))
        if d:
            self.out_dir.setText(d)

    def _fill_table(self, records):
        self.table.setRowCount(len(records))
        for row, r in enumerate(records):
            vals = [
                r["name"], r["phase"], f"{r['ready']}/{r['total']}", str(r["restarts"]),
                r["reason"] or "—", r["node"] or "—", r["host_ip"] or "—",
                r["pod_ip"] or "—", r["age"], r["sev"],
            ]
            for col, v in enumerate(vals):
                item = QTableWidgetItem(v)
                if col == 0:
                    item.setToolTip("; ".join("%s:%s" % (lv, m) for lv, m in r["problems"]) or "正常")
                if _SEV_FG.get(r["sev"]) and col in (0, 9):
                    item.setForeground(QColor(_SEV_FG[r["sev"]]))
                    if col == 9:
                        item.setFont(self._bold())
                self.table.setItem(row, col, item)
        for row, r in enumerate(records):
            if r["sev"] != "OK":
                self.table.selectRow(row)
                self._show_log(row)
                return
        if records:
            self.table.selectRow(0)
            self._show_log(0)

    def _bold(self):
        from PyQt6.QtGui import QFont
        f = QFont(); f.setBold(True); return f

    def _on_cell_clicked(self, row, _col):
        self._show_log(row)

    def _show_log(self, row):
        if not (0 <= row < len(self._records)) or not self._out_dir:
            return
        rec = self._records[row]
        name = rec["name"]
        logs_dir = Path(self._out_dir) / "logs"
        parts = []
        containers = rec.get("containers") or [None]
        for c in containers:
            p = logs_dir / ("%s__%s.log" % (name, c)) if c else logs_dir / ("%s.log" % name)
            if p.exists():
                txt = p.read_text(encoding="utf-8", errors="replace")
                parts.append(("[%s]\n" % (c or "main")) + txt if len(containers) > 1 else txt)
        self.log_view.setPlainText("\n".join(parts) if parts else "（该 Pod 无日志文件）")

    def _open_report(self) -> None:
        if self._report:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._report))

    def _open_dir(self) -> None:
        if self._out_dir:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._out_dir))
