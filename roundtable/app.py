"""指定議題を変更せず表示するデスクトップ画面。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys

from . import app_actions
from .ledger import LedgerError
from .local_read import read_local_file
from .read_model import export_topic


def read_safe_export(path: Path | None) -> dict:
    if path is None:
        return {"status": "unknown", "reason": "Safe Resume export未指定"}
    try:
        value = json.loads(read_local_file(path, 8 * 1024 * 1024).decode("utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("非対応のexport形式です")
        if value.get("provider") != "codex":
            raise ValueError("Safe ResumeのCodex exportではありません")
        return value
    except (OSError, ValueError) as exc:
        return {"status": "unknown", "reason": str(exc)}


def seat_rows(snapshot: dict) -> list[tuple[str, str, str, str]]:
    return [
        (str(seat.get("provider", "unknown")),
         str(seat.get("title") or seat.get("participant") or "名称不明"),
         str(seat.get("conversation_id") or "不明"),
         str(seat.get("state") or "unknown"))
        for seat in snapshot.get("seats", [])
    ]


def run(root: Path, slug: str, safe_export: Path | None = None) -> int:
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError:
        print("GUIにはTk対応のPythonが必要です。exportコマンドはTkなしで利用できます。")
        return 2

    try:
        window = tk.Tk()
    except tk.TclError:
        print("GUIを表示できるデスクトップがありません。exportを利用してください。")
        return 2
    executor = ThreadPoolExecutor(max_workers=1)
    pending = None
    snapshot = {}
    window.title("AI Round Table — 会話と議題")
    window.geometry("1080x720")
    window.minsize(760, 480)
    frame = ttk.Frame(window, padding=16)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="AI Round Table", font=("", 22, "bold")).pack(anchor="w")
    ttk.Label(frame, text=f"議題: {slug}").pack(anchor="w", pady=(4, 8))
    status = tk.StringVar()
    ttk.Label(frame, textvariable=status, wraplength=980).pack(anchor="w")
    controls = ttk.Frame(frame)
    controls.pack(fill="x", pady=(6, 0))
    tabs = ttk.Notebook(frame)
    tabs.pack(fill="both", expand=True, pady=10)
    seats = ttk.Frame(tabs, padding=8)
    tabs.add(seats, text="会話 · Codex / Claude Code")
    table = ttk.Treeview(seats, columns=("provider", "title", "id", "state"), show="headings")
    for key, title, width in (("provider", "接続先", 120), ("title", "席", 150),
                              ("id", "元の会話ID", 420), ("state", "観測状態", 150)):
        table.heading(key, text=title)
        table.column(key, width=width)
    table.pack(fill="both", expand=True)
    ttk.Label(seats, text="指定議題に登録された席だけを表示します。会話本文・全履歴の探索は行いません。",
              wraplength=900).pack(anchor="w", pady=8)
    texts = []
    for title in ("円卓 · 送達と回収", "議事録", "再開 · Safe Resume"):
        container = ttk.Frame(tabs, padding=8)
        tabs.add(container, text=title)
        text = tk.Text(container, wrap="word", font=("", 12))
        scrollbar = ttk.Scrollbar(container, command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        texts.append(text)

    def refresh() -> None:
        nonlocal snapshot
        snapshot = export_topic(root, slug)
        status.set(f"取得: {snapshot.get('observed_at', '不明')}  ·  "
                   f"{snapshot.get('status', 'unknown')}  {snapshot.get('reason') or ''}")
        table.delete(*table.get_children())
        for row in seat_rows(snapshot):
            table.insert("", "end", values=row)
        values = [
            "受付・送達・回答完成・回収・人間の採否は別の状態です。\n\n"
            + json.dumps(snapshot.get("invocations", []), ensure_ascii=False, indent=2),
            snapshot.get("minutes") or "議事録を取得できません。",
            "固定したexportファイルの表示です。現在の接続や再開成功を保証しません。\n\n"
            + json.dumps(read_safe_export(safe_export), ensure_ascii=False, indent=2),
        ]
        for text, value in zip(texts, values):
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.insert("1.0", value)
            text.configure(state="disabled")

    def submit(action) -> None:
        nonlocal pending
        if pending is not None:
            return
        for button in buttons:
            button.state(["disabled"])
        status.set("処理中…")
        pending = executor.submit(action)
        window.after(80, finish)

    def finish() -> None:
        nonlocal pending
        if not pending.done():
            window.after(80, finish)
            return
        try:
            detail = pending.result()
        except (OSError, ValueError, RuntimeError, LedgerError) as exc:
            detail = f"処理を確認できません。記録を確認してください。\n{exc}"
        finally:
            pending = None
            for button in buttons:
                button.state(["!disabled"])
        refresh()
        messagebox.showinfo("操作結果", detail, parent=window)

    def ask_text(title: str, prompt: str) -> str | None:
        dialog = tk.Toplevel(window)
        dialog.title(title)
        dialog.transient(window)
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=prompt, wraplength=500).pack(anchor="w")
        value = tk.StringVar()
        entry = ttk.Entry(frame, width=56, textvariable=value)
        entry.pack(fill="x", pady=8)
        result = None

        def accept(_event=None):
            nonlocal result
            result = value.get()
            dialog.destroy()

        ttk.Button(frame, text="確認", command=accept).pack(side="right")
        ttk.Button(frame, text="キャンセル", command=dialog.destroy).pack(side="right")
        entry.bind("<Return>", accept)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.wait_visibility()
        dialog.grab_set()
        entry.focus_set()
        window.wait_window(dialog)
        return result

    def selected_invocation():
        records = {record["id"]: record for record in snapshot.get("invocations", [])}
        if not records:
            messagebox.showinfo("依頼", "先に依頼を準備してください。", parent=window)
            return None
        inv = ask_text("依頼を選ぶ", "円卓タブに表示された依頼ID")
        if inv is None:
            return None
        record = records.get(inv)
        if record is None:
            messagebox.showerror("依頼", "表示中の依頼IDを指定してください。", parent=window)
        return record

    def prepare(mode: str) -> None:
        participant = ask_text("依頼を準備", "席 (codex / claude / cc / claude-code)")
        if participant not in ("codex", "claude", "cc", "claude-code"):
            return
        submit(lambda: app_actions.report(app_actions.prepare_request(root, slug, participant, mode)))

    def review_send() -> None:
        record = selected_invocation()
        if record is None:
            return
        if record["state"] != "prepared" or record.get("receipt", {}).get("state") not in (None, "prepared"):
            messagebox.showinfo("送信", "準備済みの依頼だけ送信できます。", parent=window)
            return
        provider = "codex" if record["participant"] == "codex" else "claude"
        if record["participant"] not in ("codex", "claude", "cc", "claude-code"):
            return
        if provider == "claude" and sys.platform == "win32":
            messagebox.showinfo("Claude Code", "Windowsでは既存Claude Desktopへのsocket送信に対応していません。", parent=window)
            return
        target = ask_text("宛先を指定", f"{provider} の既存会話UUID")
        if target is None:
            return
        try:
            frozen = app_actions.preview(root, slug, provider, record["id"], target)
        except (OSError, ValueError) as exc:
            messagebox.showerror("プレビュー不可", str(exc), parent=window)
            return
        dialog = tk.Toplevel(window)
        dialog.title("宛先と依頼を確認")
        dialog.geometry("800x580")
        dialog.transient(window)
        dialog.grab_set()
        ttk.Label(dialog, text=f"{provider}  ·  {frozen.target}").pack(anchor="w", padx=12, pady=8)
        ttk.Label(dialog, text="Claudeは既存の搬送形式で依頼を包み、照合用IDを付けます。").pack(anchor="w", padx=12)
        bar = ttk.Frame(dialog, padding=8)
        bar.pack(side="bottom", fill="x")
        box = tk.Text(dialog, wrap="word")
        box.pack(fill="both", expand=True, padx=12, pady=8)
        box.insert("1.0", frozen.packet + "\n\n── 議事録の固定版 ──\n\n" + frozen.snapshot)
        box.configure(state="disabled")

        def approve() -> None:
            dialog.destroy()
            submit(lambda: frozen.send(root, slug))

        ttk.Button(bar, text="キャンセル", command=dialog.destroy).pack(side="right")
        ttk.Button(bar, text="確認して一度だけ送信", command=approve).pack(side="right")

    def collect() -> None:
        record = selected_invocation()
        if record is None:
            return
        if record["participant"] == "codex" and record["state"] == "prepared":
            messagebox.showinfo("回収", "送信が始まった依頼を指定してください。", parent=window)
            return
        args = ("socket-collect", "--invocation", record["id"]) if record["participant"] in (
            "claude", "cc", "claude-code"
        ) else ("collect", "--invocation", record["id"], "--timeout", "0")
        submit(lambda: app_actions.report(app_actions.execute(root, slug, *args)))

    def decide() -> None:
        records = snapshot.get("invocations", [])
        if not any(record["state"] == "merged" for record in records):
            messagebox.showinfo("採否", "判断する回答を先に議事録へ回収してください。", parent=window)
            return
        unresolved = sum(record["state"] != "merged" for record in records)
        verdict = ask_text("座長の採否", f"採用・却下・保留と理由を入力（未回収 {unresolved} 件）")
        if verdict and verdict.strip() and messagebox.askyesno(
            "採否を記録", f"この議題を閉じ、判断を議事録に記録します。\n\n{verdict}", parent=window
        ):
            submit(lambda: app_actions.report(app_actions.execute(root, slug, "close", "--verdict=" + verdict)))

    buttons = []
    for title, action in (("再読込み", refresh), ("依頼を準備", lambda: prepare("通常")), ("送信内容を確認", review_send),
                          ("回答を回収", collect), ("座長の採否", decide),
                          ("反証", lambda: prepare("反証")), ("振り返り", lambda: prepare("振り返り"))):
        button = ttk.Button(controls, text=title, command=action)
        button.pack(side="left", padx=(0, 6))
        buttons.append(button)

    def close() -> None:
        if pending is not None:
            return
        window.destroy()

    window.protocol("WM_DELETE_WINDOW", close)
    try:
        refresh()
        window.mainloop()
    finally:
        executor.shutdown()
    return 0
