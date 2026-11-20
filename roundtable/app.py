"""指定議題を変更せず表示するデスクトップ画面。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys

from . import app_actions, claude_socket
from .ledger import LedgerError
from .local_read import read_local_file
from .read_model import export_topic
from .paths import topic_dir
from .relay import RelayError, rename_registered_seat


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


def visible_seats(seats: list[dict], query: str, *, descending: bool = False) -> list[dict]:
    """登録済みの席を表示名で検索・整列し、元の識別子は保持する。"""
    from unicodedata import normalize

    def key(value) -> str:
        return normalize("NFKC", str(value or "")).casefold()

    needle = key(query.strip())
    matches = [seat for seat in seats if any(
        needle in key(seat.get(field)) for field in ("title", "participant", "provider", "conversation_id", "project_title")
    )]
    return sorted(matches, key=lambda seat: (key(seat.get("title")), seat["id"]), reverse=descending)


def readable_minutes(raw: str) -> str:
    """議事録の本文から表示用の文面を作る。根拠表と照合記録は原本で読める。"""
    import re

    lines = raw.splitlines()
    verdict = ""
    if lines and lines[0] == "---" and "---" in lines[1:]:
        end = lines.index("---", 1)
        verdict = next((line.removeprefix("verdict:").strip()
                        for line in lines[1:end] if line.startswith("verdict:")), "")
        lines = lines[end + 1:]

    result = []
    in_evidence = False
    for line in lines:
        if line == "| claim | evidence_type | evidence |":
            in_evidence = True
            continue
        if in_evidence:
            if line.startswith("|"):
                continue
            in_evidence = False
        if re.fullmatch(r"<!-- roundtable-response-sha256: [a-f0-9]{64} -->", line):
            continue
        answer = re.fullmatch(r"### (.+) \(invocation: [a-f0-9]{12}\)", line)
        round_heading = re.fullmatch(r"## Round (\d+)", line)
        if answer:
            line = f"### {answer[1]} の回答"
        elif round_heading:
            line = f"## ラウンド {round_heading[1]}"
        result.append(line)
    if verdict:
        result.extend(("", "## 座長の採否", "", verdict))
    return "\n".join(result).strip()


def observed_claude_projects(root: Path) -> list[dict]:
    """Claude本体で照合したプロジェクトとスレッドの読取り用スナップショット。"""
    try:
        projects = json.loads(read_local_file(root / "claude-projects-view.json", 1024 * 1024))
    except (OSError, ValueError):
        return []
    if not isinstance(projects, list):
        return []
    rows = []
    seen = set()
    for project in projects:
        if not isinstance(project, dict):
            continue
        project_id, title, threads = (project.get(key) for key in ("id", "title", "threads"))
        if (not isinstance(project_id, str) or not project_id.startswith("chan_")
                or project_id in seen or not isinstance(title, str) or not isinstance(threads, list)):
            continue
        seen.add(project_id)
        for index, thread in enumerate(threads):
            if not isinstance(thread, str) or not thread.strip():
                continue
            rows.append({"id": f"observed:{project_id}:{index}", "provider": "claude",
                         "title": thread, "project_id": project_id, "project_title": title,
                         "state": "閲覧用", "catalog_entry": True})
    return rows


def local_codex_catalog(index: Path) -> list[dict]:
    """ローカルの表示名索引を読む。会話本文や送信権限は含まない。"""
    from uuid import UUID

    try:
        rows = read_local_file(index, 8 * 1024 * 1024).splitlines()
    except (OSError, ValueError):
        return []
    chats = {}
    for line in rows:
        try:
            record = json.loads(line)
            native_id = str(UUID(record["id"]))
            title = record["thread_name"]
            if not isinstance(title, str) or not title.strip():
                continue
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        chats[native_id] = {
            "id": f"codex-index:{native_id}", "provider": "codex", "title": title.strip(),
            "conversation_id": native_id, "state": "閲覧用", "catalog_entry": True,
        }
    return list(chats.values())


def development_view() -> str:
    """実行中の円卓ソースの状態を Git の読取りで表示する。"""
    import subprocess

    checkout = Path(__file__).resolve().parent.parent
    def git(*args: str) -> str:
        result = subprocess.run(["git", "-C", str(checkout), *args], capture_output=True,
                                text=True, timeout=3, check=True)
        return result.stdout.strip()

    try:
        root = Path(git("rev-parse", "--show-toplevel"))
        branch = git("branch", "--show-current") or "ブランチ未選択"
        commit = git("log", "-1", "--format=%h %s")
        changed = git("status", "--short")
    except (OSError, subprocess.SubprocessError, ValueError):
        return "円卓ソースの作業ツリーを確認できません。"
    return (f"円卓ソースの開発状況\n\n作業ツリー  {root}\nブランチ  {branch}\n"
            f"最新コミット  {commit}\n\n未コミットの変更\n{changed or 'なし'}")


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
    shell = ttk.Frame(window)
    shell.pack(fill="both", expand=True)
    sidebar = tk.Frame(shell, background="#172326", padx=16, pady=20, width=220)
    sidebar.pack(side="left", fill="y")
    sidebar.pack_propagate(False)
    tk.Label(sidebar, text="円卓", font=("", 19, "bold"),
             background="#172326", foreground="#f5f3ea").pack(anchor="w")
    tk.Label(sidebar, text="この保存先の話題", background="#172326",
             foreground="#b5c8c4").pack(anchor="w", pady=(20, 5))
    topics = tk.Listbox(sidebar, exportselection=False, borderwidth=0, highlightthickness=1,
                        highlightbackground="#526866", background="#223235", foreground="#f5f3ea",
                        selectbackground="#37635c", selectforeground="#ffffff", activestyle="none")
    topics.pack(fill="both", expand=True)
    topic_directory = root / "minutes"
    names = sorted(
        entry.name for entry in topic_directory.iterdir()
        if entry.is_dir() and not entry.is_symlink() and (entry / "minutes.md").is_file()
    ) if topic_directory.is_dir() and not topic_directory.is_symlink() else []
    if slug not in names:
        names.append(slug)
    def refresh_topic_labels() -> None:
        topics.delete(0, "end")
        for name in names:
            label = name
            try:
                path = topic_dir(root, name) / "seats.json"
                registered = json.loads(read_local_file(path, 1024 * 1024))
                first = next((seat for seat in registered.values() if isinstance(seat, dict)
                              and seat.get("thread_ref")), {})
                label = first.get("display_title") or first.get("title") or first.get("thread_name") or name
            except (OSError, ValueError, AttributeError):
                pass
            topics.insert("end", label)
        topics.selection_set(names.index(slug))

    refresh_topic_labels()

    frame = ttk.Frame(shell, padding=(24, 18))
    frame.pack(side="left", fill="both", expand=True)
    ttk.Label(frame, text="AI ROUND TABLE  /  議題").pack(anchor="w")
    title_var = tk.StringVar(value=slug)
    ttk.Label(frame, textvariable=title_var, font=("", 21, "bold"), wraplength=660).pack(
        anchor="w", pady=(5, 6)
    )
    status = tk.StringVar()
    ttk.Label(frame, textvariable=status, wraplength=980).pack(anchor="w")
    controls = ttk.Frame(frame)
    controls.pack(fill="x", pady=(6, 0))
    tabs = ttk.Notebook(frame)
    tabs.pack(fill="both", expand=True, pady=10)
    tabs.enable_traversal()
    seats = ttk.Frame(tabs, padding=8)
    tabs.add(seats, text="参加チャット")
    provider_bar = ttk.Frame(seats)
    provider_bar.pack(fill="x", pady=(0, 8))
    selected_provider = tk.StringVar(value="codex")
    for provider, label in (("codex", "Codex"), ("claude", "Claude Code"), ("antigravity", "Antigravity")):
        ttk.Radiobutton(provider_bar, text=label, variable=selected_provider, value=provider).pack(
            side="left", padx=(0, 14)
        )
    provider_hint = tk.StringVar()
    ttk.Label(seats, textvariable=provider_hint, wraplength=660).pack(side="bottom", anchor="w", pady=8)
    seat_filter = ttk.Frame(seats)
    seat_filter.pack(fill="x", pady=(0, 6))
    query = tk.StringVar()
    ttk.Label(seat_filter, text="チャットを検索").pack(side="left", padx=(0, 8))
    ttk.Entry(seat_filter, textvariable=query).pack(side="left", fill="x", expand=True)
    descending = False
    seat_list = ttk.Frame(seats)
    seat_list.pack(fill="both", expand=True)
    table = ttk.Treeview(seat_list, columns=("provider", "state"),
                         show="tree headings", selectmode="browse")
    table.column("#0", width=460, minwidth=200)
    table.heading("provider", text="接続先")
    table.column("provider", width=100, stretch=False)
    table.heading("state", text="状態")
    table.column("state", width=100, stretch=False)
    scroll = ttk.Scrollbar(seat_list, command=table.yview)
    table.configure(yscrollcommand=scroll.set)
    scroll.pack(side="right", fill="y")
    table.pack(side="left", fill="both", expand=True)

    texts = []
    for title in ("議事録", "再開 · Safe Resume", "開発状況"):
        container = ttk.Frame(tabs, padding=8)
        tabs.add(container, text=title)
        text = tk.Text(container, wrap="word", font=("", 12))
        scrollbar = ttk.Scrollbar(container, command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        texts.append(text)

    def render_seats(*_args) -> None:
        selected = table.selection()
        catalog = "catalog:claude"
        previous_groups = {item: table.item(item, "open")
                           for item in table.get_children(catalog)} if table.exists(catalog) else {}
        catalog_open = table.exists(catalog) and table.item(catalog, "open")
        codex_open = table.exists("catalog:codex") and table.item("catalog:codex", "open")
        table.delete(*table.get_children())
        provider = selected_provider.get()
        all_seats = [seat for seat in snapshot.get("seats", []) if seat.get("provider") == provider]
        hints = {
            "codex": "登録済みの席を先頭に表示します。ローカル会話は名前で探せます。",
            "claude": "確認できたプロジェクトとローカル会話を表示します。",
            "antigravity": "新しい依頼から、公式CLIで Gemini 4 Argon に壁打ちを依頼できます。",
        }
        provider_hint.set(hints[provider] if all_seats or provider == "antigravity" else "この接続先で表示できるチャットはありません。")
        visible = visible_seats(all_seats, query.get(), descending=descending)
        by_id = {seat["id"]: seat for seat in all_seats}
        groups = {}
        project_catalog = "catalog:claude-projects"
        codex_catalog = "catalog:codex"

        def add(seat, ancestors):
            key = seat["id"]
            if table.exists(key):
                return
            if seat.get("catalog_entry") and seat.get("provider") == "codex":
                if not table.exists(codex_catalog):
                    table.insert("", "end", iid=codex_catalog, text="Codexのローカル会話",
                                 open=bool(query.get().strip()) or codex_open)
                table.insert(codex_catalog, "end", iid=key, text=seat["title"],
                             values=("codex", "閲覧用"))
                return
            project_id = seat.get("project_id")
            if project_id:
                if not table.exists(project_catalog):
                    table.insert("", "end", iid=project_catalog, text="Claude Codeのプロジェクト",
                                 open=True)
                parent = "project:" + project_id
                if not table.exists(parent):
                    table.insert(project_catalog, "end", iid=parent, text=seat["project_title"], open=True)
                table.insert(parent, "end", iid=key, text=seat["title"], values=("claude", "閲覧用"))
                return
            parent = catalog if seat.get("catalog_entry") else ""
            if parent and not table.exists(catalog):
                table.insert("", "end", iid=catalog, text="Claude Codeのローカル会話（作業フォルダ）",
                             open=bool(query.get().strip()) or catalog_open)
            workspace = seat.get("workspace")
            if workspace:
                if workspace not in groups:
                    group = "workspace:" + workspace
                    groups[workspace] = group
                    table.insert(parent, "end", iid=group, text=Path(workspace).name or workspace,
                                 open=bool(query.get().strip()) or previous_groups.get(group, False))
                parent = groups[workspace]
            upstream = by_id.get(seat.get("parent_chat_id"))
            if upstream and workspace and upstream.get("workspace") == workspace:
                upstream_id = upstream["id"]
                if upstream_id not in ancestors and upstream_id != key:
                    add(upstream, ancestors | {key})
                    parent = upstream_id
            row, = seat_rows({"seats": [seat]})
            table.insert(parent, "end", iid=key, text=row[1], values=(row[0], row[3]), open=True)

        for seat in sorted(visible, key=lambda item: (bool(item.get("catalog_entry")), not bool(item.get("project_id")))):
            add(seat, set())
        if selected and table.exists(selected[0]):
            table.selection_set(selected[0])

    def sort_seats() -> None:
        nonlocal descending
        descending = not descending
        table.heading("#0", text="チャット名 ▼" if descending else "チャット名 ▲")
        render_seats()

    table.heading("#0", text="チャット名 ▲", command=sort_seats)
    query.trace_add("write", render_seats)
    selected_provider.trace_add("write", render_seats)

    def refresh() -> None:
        nonlocal snapshot
        snapshot = export_topic(root, slug)
        if sys.platform == "darwin":
            snapshot["seats"] = [
                seat for seat in snapshot.get("seats", []) if not seat.get("catalog_entry")
            ] + observed_claude_projects(root) + [dict(seat, catalog_entry=True) for seat in claude_socket.list_desktop_chats()]
        registered_ids = {seat.get("conversation_id") for seat in snapshot.get("seats", [])
                          if not seat.get("catalog_entry") and seat.get("provider") == "codex"}
        snapshot["seats"] = snapshot.get("seats", []) + [
            seat for seat in local_codex_catalog(Path.home() / ".codex" / "session_index.jsonl")
            if seat["conversation_id"] not in registered_ids
        ]
        status.set(f"取得: {snapshot.get('observed_at', '不明')}  ·  "
                   f"{snapshot.get('status', 'unknown')}  {snapshot.get('reason') or ''}")
        raw_minutes = snapshot.get("minutes") or ""
        visible_minutes = readable_minutes(raw_minutes)
        heading = visible_minutes.splitlines()[0] if visible_minutes else ""
        title_var.set(heading[2:] if heading.startswith("# ") else slug)
        refresh_topic_labels()
        render_seats()
        values = [
            visible_minutes or "議事録を取得できません。",
            "固定したexportファイルの表示です。現在の接続や再開成功を保証しません。\n\n"
            + json.dumps(read_safe_export(safe_export), ensure_ascii=False, indent=2),
            development_view(),
        ]
        for text, value in zip(texts, values):
            text.configure(state="normal")
            text.delete("1.0", "end")
            if text is texts[0]:
                text.configure(background="#faf8f1", foreground="#202e2c", padx=20, pady=16,
                               spacing1=4, spacing3=5, selectbackground="#b6d4c7")
                text.tag_configure("section", font=("", 15, "bold"), foreground="#28534c",
                                   spacing1=13, spacing3=6)
                text.tag_configure("speaker", font=("", 13, "bold"), foreground="#345655", spacing1=9)
                for line in value.splitlines():
                    if line.startswith("### "):
                        text.insert("end", line[4:] + "\n", "speaker")
                    elif line.startswith("## "):
                        text.insert("end", line[3:] + "\n", "section")
                    elif not line.startswith("# "):
                        text.insert("end", line + "\n")
            else:
                text.insert("1.0", value)
            text.configure(state="disabled")

    def submit(action, on_success=None) -> None:
        nonlocal pending
        if pending is not None:
            return
        for button in buttons:
            button.state(["disabled"])
        status.set("処理中…")
        pending = executor.submit(action)
        window.after(80, finish, on_success)

    def finish(on_success) -> None:
        nonlocal pending
        if not pending.done():
            window.after(80, finish, on_success)
            return
        succeeded = False
        try:
            detail = pending.result()
            succeeded = True
        except (OSError, ValueError, RuntimeError, LedgerError, RelayError) as exc:
            detail = f"処理を確認できません。記録を確認してください。\n{exc}"
        finally:
            pending = None
            for button in buttons:
                button.state(["!disabled"])
        refresh()
        if succeeded and on_success is not None:
            on_success(detail)
        else:
            messagebox.showinfo("操作結果", detail, parent=window)

    def ask_text(title: str, prompt: str, choices: tuple[str, ...] = (), initial: str = "") -> str | None:
        dialog = tk.Toplevel(window)
        dialog.title(title)
        dialog.transient(window)
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=prompt, wraplength=500).pack(anchor="w")
        value = tk.StringVar(value=choices[0] if choices else initial)
        if choices:
            entry = ttk.Combobox(frame, width=56, textvariable=value, values=choices, state="readonly")
        else:
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
        options = {f"{r['participant']}  ·  {r['state']}  ·  {key}": key for key, r in records.items()}
        choice = ask_text("依頼を選ぶ", "操作する依頼を選んでください", tuple(options))
        inv = options.get(choice)
        if inv is None:
            return None
        record = records.get(inv)
        if record is None:
            messagebox.showerror("依頼", "表示中の依頼IDを指定してください。", parent=window)
        return record

    def prepare(mode: str) -> None:
        supported = ("codex", "claude", "cc", "claude-code", "antigravity")
        selected = table.selection()
        seat = next((item for item in snapshot.get("seats", [])
                     if selected and item["id"] == selected[0]), {})
        participant = seat.get("participant")
        if participant not in supported:
            participant = ask_text("依頼を準備", "席を選んでください", ("codex", "claude", "antigravity"))
        if participant not in supported:
            return
        question = ask_text("新しい依頼", "相手に伝えたい質問や依頼を入力してください。")
        if not question or not question.strip():
            return

        def show_prepared(invocation):
            record = next((item for item in snapshot.get("invocations", [])
                           if item["id"] == invocation), None)
            if record is None:
                messagebox.showinfo("依頼", "依頼を準備しました。送信内容を確認から開けます。", parent=window)
                return
            review_send(record)

        submit(lambda: app_actions.prepared_invocation(
            app_actions.prepare_request(root, slug, participant, mode, question=question)
        ), show_prepared)

    def review_send(record=None) -> None:
        if record is None:
            record = selected_invocation()
        if record is None:
            return
        if record["state"] != "prepared" or record.get("receipt", {}).get("state") not in (None, "prepared"):
            messagebox.showinfo("送信", "準備済みの依頼だけ送信できます。", parent=window)
            return
        participant = record["participant"]
        provider = participant if participant in ("codex", "antigravity") else "claude"
        if record["participant"] not in ("codex", "claude", "cc", "claude-code", "antigravity"):
            return
        if provider == "claude" and sys.platform == "win32":
            messagebox.showinfo("Claude Code", "Windowsでは既存Claude Desktopへのsocket送信に対応していません。", parent=window)
            return
        targets = tuple(dict.fromkeys(
            seat["conversation_id"] for seat in snapshot.get("seats", [])
            if seat.get("participant") == record["participant"]
            and isinstance(seat.get("conversation_id"), str) and seat["conversation_id"]
        ))
        if provider == "antigravity":
            target = app_actions.ANTIGRAVITY_MODEL
        elif provider == "claude" and sys.platform == "darwin":
            candidates = claude_socket.list_desktop_chats()
            selected = table.selection()
            if selected:
                chosen = next((seat for seat in candidates if seat["id"] == selected[0]), None)
            else:
                chosen = None
            if chosen is None:
                choices = {
                    f"{seat['title']}  ·  {seat['state']}  ·  {seat['conversation_id']}": seat
                    for seat in candidates
                }
                if not choices:
                    messagebox.showinfo("Claude Code", "表示できるローカル会話がありません。", parent=window)
                    return
                choice = ask_text("Claude Codeの宛先", "既存会話を選んでください", tuple(choices))
                chosen = choices.get(choice)
                if chosen is None:
                    return
            target = chosen["send_target"]
            if target is None:
                messagebox.showinfo("Claude Code", "Claude Desktopでこの会話を開き、円卓を更新してください。",
                                    parent=window)
                return
        else:
            target = ask_text("宛先を指定", f"{provider} の既存会話UUID", targets)
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
        seat_name = next((seat["title"] for seat in snapshot.get("seats", [])
                          if seat.get("conversation_id") == frozen.target), provider)
        ttk.Label(dialog, text="送信先", font=("", 11)).pack(anchor="w", padx=16, pady=(12, 2))
        ttk.Label(dialog, text=seat_name, font=("", 16, "bold"), wraplength=740).pack(anchor="w", padx=16)
        ttk.Label(dialog, text=frozen.target).pack(anchor="w", padx=16, pady=(2, 8))
        if provider == "claude":
            ttk.Label(dialog, text="Claudeの搬送時に照合用IDを付けます。").pack(anchor="w", padx=12)
        bar = ttk.Frame(dialog, padding=8)
        bar.pack(side="bottom", fill="x")
        box = tk.Text(dialog, wrap="word")
        box.pack(fill="both", expand=True, padx=12, pady=8)
        desktop = provider == "codex" and any(
            seat.get("transport") in ("codex-desktop", "codex-queue") and seat.get("conversation_id") == frozen.target
            for seat in snapshot.get("seats", [])
        )
        if provider == "antigravity":
            box.insert("1.0", frozen.antigravity_body(root, slug))
        elif desktop:
            box.insert("1.0", frozen.desktop_body(root, slug))
        else:
            box.insert("1.0", frozen.packet + "\n\n── 議事録の固定版 ──\n\n" + frozen.snapshot)
        box.configure(state="disabled")

        def approve() -> None:
            dialog.destroy()
            submit(lambda: frozen.send(root, slug))

        ttk.Button(bar, text="キャンセル", command=dialog.destroy).pack(side="right")
        ttk.Button(bar, text="確認して一度だけ送信", command=approve, default="active").pack(side="right")

    def collect() -> None:
        record = selected_invocation()
        if record is None:
            return
        if record["participant"] == "codex":
            from .journal import Journal
            from .paths import ensure_topic

            current = Journal.load(ensure_topic(root, slug)).data["invocations"].get(record["id"], {})
            if current.get("codex_queue_claim"):
                from .codex_queue import recover

                submit(lambda: recover(root, slug, record["id"]))
                return
        if record["participant"] == "codex" and record["state"] == "prepared":
            messagebox.showinfo("回収", "送信が始まった依頼を指定してください。", parent=window)
            return
        args = ("socket-collect", "--invocation", record["id"]) if record["participant"] in (
            "claude", "cc", "claude-code"
        ) else ("collect", "--invocation", record["id"], "--timeout", "0")
        submit(lambda: app_actions.report(app_actions.execute(root, slug, *args)))

    def continue_discussion() -> None:
        note = ask_text("座長判断・次の論点", "議事録に残し、次の依頼で伝えたい判断や質問")
        if note and note.strip():
            tabs.select(1)
            submit(lambda: app_actions.report(
                app_actions.execute(root, slug, "chair-note", "--note=" + note)
            ))

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

    def rename_chat() -> None:
        registered = export_topic(root, slug).get("seats", [])
        selected = table.selection()
        seat = next((item for item in registered if selected and item["id"] == selected[0]), None)
        if seat is None and not selected:
            seat = next((item for item in registered if item.get("conversation_id")), None)
        if seat is None or not isinstance(seat.get("conversation_id"), str):
            messagebox.showinfo("チャット名", "この議題に登録済みのチャットを選んでください。", parent=window)
            return
        title = ask_text("円卓のチャット表示名", "円卓で使う表示名を入力してください。",
                         initial=seat["title"])
        if title is not None:
            def rename():
                from .paths import ensure_topic

                rename_registered_seat(ensure_topic(root, slug), seat["id"], seat["conversation_id"], title)
                return "円卓の表示名を更新しました。"
            submit(rename)

    buttons = []
    for title, action in (("新しい依頼", lambda: prepare("通常")),
                          ("送信内容を確認", review_send), ("議事録を見る", lambda: (refresh(), tabs.select(1)))):
        button = ttk.Button(controls, text=title, command=action)
        button.pack(side="left", padx=(0, 6))
        buttons.append(button)

    more = ttk.Menubutton(controls, text="その他")
    menu = tk.Menu(more, tearoff=False)
    for title, action in (("再読込み", refresh), ("回答を回収", collect),
                          ("判断を残して続ける", continue_discussion), ("採否を確定", decide), ("チャット表示名を変更", rename_chat)):
        menu.add_command(label=title, command=action)
    menu.add_separator()
    for mode in ("反証", "振り返り", *app_actions.RESEARCH_MODES):
        menu.add_command(label=mode, command=lambda selected=mode: prepare(selected))
    more.configure(menu=menu)
    more.pack(side="left")
    buttons.append(more)

    def switch_topic(_event) -> None:
        nonlocal slug
        selected = topics.curselection()
        if not selected:
            return
        if pending is not None:
            topics.selection_clear(0, "end")
            topics.selection_set(names.index(slug))
            return
        destination = names[selected[0]]
        if destination == slug:
            return
        slug = destination
        table.selection_remove(*table.selection())
        query.set("")
        refresh()
        tabs.select(1)

    topics.bind("<<ListboxSelect>>", switch_topic)

    def close() -> None:
        if pending is not None:
            return
        window.destroy()

    window.protocol("WM_DELETE_WINDOW", close)
    try:
        refresh()
        tabs.select(1)
        window.mainloop()
    finally:
        executor.shutdown()
    return 0
