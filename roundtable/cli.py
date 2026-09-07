"""roundtable CLI — new-topic / dispatch / collect / status / close。

`python -m roundtable.cli <cmd>` で起動する。dispatch は AI を実行しない:
packet を生成して relay (既定 Tier3 クリップボード / 任意 Tier1) に載せ、
scratch の出力を待つだけ。貼り付け先の chat 操作は人間 (CEO) が行う
(DESIGN v6 §0)。Tier1 障害時は Tier3 に縮退し、Tier2 へは昇格しない。
"""
import argparse
import json
import math
import sys
import time
from pathlib import Path

from . import minutes, packet, watcher
from .filelock import LockTimeout
from .journal import Journal
from .ledger import LedgerDirtyError
from .paths import ensure_topic, topic_dir
from .relay import DeliveryUnknownError, get_relay, load_seats, save_seats
from .review_workflow import load_manifest, validate_live_git, validate_review_workflow


def _write_last_result(tp, payload: dict) -> None:
    minutes.atomic_write(tp.last_result, json.dumps(payload, ensure_ascii=False, indent=1))


def _try_write_last_result(args, payload: dict) -> None:
    """議題が特定できる時だけ last-result.json を残す (S2 の機械確認経路を切らさない)。

    例外ハンドラからの最後の手当なので、ここで更に失敗しても新しい例外は投げない。
    握り潰しではない: 呼び出し側が既に stderr へ本体の失敗を出している。
    """
    root, slug = getattr(args, "root", None), getattr(args, "slug", None)
    if not root or not slug:
        return
    try:
        _write_last_result(ensure_topic(Path(root), slug), payload)
    except (OSError, ValueError):
        pass


def _cmd_new_topic(args) -> int:
    """議題を開始する: ディレクトリ規約を作り minutes を生成する。"""
    tp = ensure_topic(Path(args.root), args.slug)
    participants = [p.strip() for p in args.participants.split(",") if p.strip()]
    minutes.create(tp, args.topic, participants, background=args.background or "")
    journal = Journal.load(tp)
    journal.record_human_action("new-topic", args.slug)
    print(f"topic 作成: {tp.minutes}")
    print(f"participants: {', '.join(participants)}")
    if args.background:
        print("background: 記録済み")
    return 0


def _cmd_set_background(args) -> int:
    """既存議題の ## 背景 を更新する (S1)。"""
    tp = ensure_topic(Path(args.root), args.slug)
    minutes.set_background(tp, args.text)
    journal = Journal.load(tp)
    journal.record_human_action("set-background", args.slug)
    print(f"background 更新: {tp.minutes}")
    return 0


def _close_relay(relay) -> None:
    """席のプロセス木を回収する (Tier3 は no-op)。

    **collect の後に呼ぶこと**: codex の `turn/start` は投げっぱなしで、返った時点では
    席がまだ書いている。collect より前に閉じると実行中の席を殺す。

    2026-08-07 レビュー H2: `close()` は実装済みなのに **本番の呼び出し元が無かった**。
    Windows は Job Object の KILL_ON_JOB_CLOSE で Python 終了時に木ごと落ちるので
    偶然助かっていたが、POSIX は `killpg` が一度も走らず、CLI 終了後に席のプロセス木
    (実測 1 席 10 プロセス) が孤児として残る。

    回収の失敗で dispatch の exit code を変えない (packet の搬出・回収はもう終わって
    いる)。ただし黙って捨てず stderr に出す。
    """
    close = getattr(relay, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception as exc:  # 回収失敗は本線の成否を変えない
        print(f"[warn] 席プロセスの回収に失敗した: {exc}", file=sys.stderr)


    # 旧 _warn_if_integrity_detection_does_not_hold は D12 で削除した。
    # grok 席が sandbox 外に書ける事実 (2026-08-07 実測) は変わらないが、検知が
    # git になったことで「証跡ごと書き換えられて照合が無意味になる」前提が消えた。
    # 席がローカル git 履歴ごと書き換える可能性は残る — 最終証跡は origin へ push
    # した履歴 (D12 の表)。


def _cmd_dispatch(args) -> int:
    """指名 1 席分の packet を出し、既定では collect まで実行する。"""
    tp = ensure_topic(Path(args.root), args.slug)
    minutes.make_snapshot(tp)
    journal = Journal.load(tp)
    inv = journal.new_invocation(args.participant, journal.round_no)
    journal.data["invocations"][inv]["delivery_route"] = (
        "stdout-only" if args.no_clipboard and args.tier == 3 else "relay"
    )
    journal.save()
    journal.record_human_action("dispatch", f"{args.participant}:{inv}")

    text = packet.build(tp, args.participant, inv, role_hint=args.role_hint)
    from .handoff import capture_request

    text = capture_request(tp, inv, text)
    print(text)

    tier = args.tier
    relay = None
    close_after_delivery_unknown = False
    try:
        if args.no_clipboard and tier == 3:
            # 明示的に搬出せず、packet を stdout のみ
            delivered = False
            relay_label = "stdout-only"
        else:
            delivery_unknown_context = None
            with watcher._invocation_lock(tp, inv):
                journal = Journal.load(tp)
                state = journal.data["invocations"][inv]["state"]
                if state != "prepared":
                    payload = {"ok": False, "reason": state,
                               "invocation": inv, "exit_code": 1}
                    _write_last_result(tp, payload)
                    print(json.dumps(payload, ensure_ascii=False))
                    return 1
                seats = load_seats(tp)
                seat_key = f"rt/{args.slug}/{args.participant}"
                seat = seats.get(seat_key, {
                    "participant": args.participant,
                    "topic": args.slug,
                    "surface": args.participant,
                    "tier": tier,
                })
                seat["topic"] = args.slug
                seat["thread_name"] = f"rt-{args.slug}-{args.participant}"
                # cwd は議題ディレクトリに限定する: Tier1 の sandbox 書込範囲がここになる。
                relay = get_relay(
                    args.participant, tier=tier, allow_fallback=True, cwd=str(tp.root.resolve())  # 相対 --root だと spawn 先で二重解決になる (P2)
                )
                try:
                    relay_label = relay.send(seat, text)
                except DeliveryUnknownError as exc:
                    # thread/start 済みなら、その参照を失うと再実行時に別席へ二重送信する。
                    seat["tier"] = relay.tier
                    seats[seat_key] = seat
                    save_seats(tp, seats)
                    journal.set_state(inv, "delivery-unknown", f"delivery-unknown: {exc}")
                    _write_last_result(
                        tp,
                        {
                            "ok": False,
                            "reason": "delivery-unknown",
                            "detail": str(exc),
                            "thread_ref": seat.get("thread_ref"),
                            "invocation": inv,
                            "exit_code": 1,
                        },
                    )
                    print(
                        f"\n[delivery-unknown] invocation: {inv} / {exc}\n"
                        "[wait] 席が受理済みの可能性があるため、自動再送せず成果物を待ちます。"
                    )
                    close_after_delivery_unknown = True
                    delivery_unknown_context = {
                        "detail": str(exc),
                        "thread_ref": seat.get("thread_ref"),
                    }
                except Exception as exc:  # Tier3 失敗など
                    journal.set_state(inv, "failed", f"relay: {exc}")
                    _write_last_result(
                        tp,
                        {
                            "ok": False,
                            "reason": "relay",
                            "detail": str(exc),
                            "invocation": inv,
                            "exit_code": 1,
                        },
                    )
                    print(f"\n[failed] invocation: {inv} / reason: relay / {exc}")
                    return 1
                else:
                    seat["tier"] = relay.tier
                    seats[seat_key] = seat
                    save_seats(tp, seats)
                    journal.set_state(inv, "delivered", f"tier{relay.tier}:{relay_label}")
                    delivered = True
                    if relay.tier == 3:
                        print("\n[clipboard] packet をクリップボードに載せた。席のチャットに貼り付けてください。")
                        journal.record_human_action("tier3_paste_required", inv)
                    else:
                        print(f"\n[tier1] packet を席へ送った (tier={relay.tier}, label={relay_label})。")
                    if "fallback" in str(relay_label):
                        print(f"[fallback] Tier1 失敗のため Tier3 に縮退: {relay_label}")
            if delivery_unknown_context is not None:
                # collect は同じ lock を取得するため、送信状態を確定してから解放する。
                return _collect_one(
                    tp, journal, inv, args.participant, args.timeout,
                    delivered=True, preserve_delivery_unknown=True,
                    failure_context=delivery_unknown_context,
                )

        if args.async_dispatch:
            _write_last_result(
                tp,
                {
                    "ok": True,
                    "reason": "async",
                    "invocation": inv,
                    "delivered": delivered,
                    "exit_code": 0,
                },
            )
            print(f"\n[async] invocation: {inv} — collect は別途 `collect` で回収")
            return 0

        return _collect_one(tp, journal, inv, args.participant, args.timeout, delivered)
    finally:
        # 通常の `--async` は「搬出だけして席を動かしたまま返る」契約なので閉じない。
        # delivery-unknown だけは async でも同じプロセスで回収を待った後に閉じる。
        if relay is not None and (not args.async_dispatch or close_after_delivery_unknown):
            _close_relay(relay)


def _leftover_tmps(tp) -> list[str]:
    """scratch に残った未確定 `.tmp` の一覧 (CEO への誤誘導を防ぐ材料)。"""
    try:
        return sorted(p.name for p in tp.scratch.glob("*.tmp"))
    except OSError:
        return []


def _collect_one(
    tp,
    journal,
    inv,
    participant,
    timeout_s,
    delivered: bool | None,
    preserve_delivery_unknown: bool = False,
    failure_context: dict | None = None,
    lock_timeout_s: float | None = None,
) -> int:
    lock_options = {} if lock_timeout_s is None else {"lock_timeout_s": lock_timeout_s}
    result = watcher.collect(
        tp,
        journal,
        inv,
        participant,
        timeout_s=timeout_s,
        preserve_delivery_unknown=preserve_delivery_unknown,
        **lock_options,
    )
    if result["ok"]:
        journal.advance_round_if_complete(minutes.parse_participants(tp))
        minutes.sync_round(tp, journal.round_no)
        payload = {
            "ok": True,
            "reason": "merged",
            "invocation": inv,
            "exit_code": 0,
            "round": journal.round_no,
        }
        if result.get("recovered") == "tmp":
            # 来歴を機械可読側にも残す: 「通常経路で成功した」と last-result.json だけ見て
            # 誤読されると、席の rename 漏れが恒久的に見えなくなる。
            payload["recovered"] = "tmp"
            payload["tmp"] = result.get("tmp")
        _write_last_result(tp, payload)
        print(f"\n[ok] merge 完了 (invocation: {inv}) / round: {journal.round_no}")
        if result.get("recovered") == "tmp":
            print(f"[recovered-from-tmp] 通常経路ではない。未確定の .tmp から回収した: {result.get('tmp')}")
            print("  席は出力を書いたが確定 (rename) していない。席側の手順漏れは未解決のまま残る。")
        return 0

    reason = result["reason"]
    payload = {
        "ok": False,
        "reason": reason,
        "invocation": inv,
        "exit_code": 1,
    }
    if result.get("tmp"):
        payload["tmp"] = result["tmp"]
    if result.get("detail"):
        payload["detail"] = result["detail"]
    if failure_context:
        payload.update(failure_context)
    _write_last_result(tp, payload)
    print(f"\n[failed] invocation: {inv} / reason: {reason}")
    if reason == "stalled-tmp":
        print(f"[stalled-tmp] 席は出力を書いたが .tmp のまま確定 (rename) されていない: {result.get('tmp')}")
        print(f"  採用しなかった理由: {result.get('detail')}")
        print("  → 無応答 (timeout) ではない。貼り付けの有無ではなく席側の確定操作を確認してください。")
    elif reason == "timeout":
        leftovers = _leftover_tmps(tp)
        if leftovers:
            print(f"[tmp 残存] scratch に未確定の .tmp がある: {', '.join(leftovers)}")
            print("  席が書きかけ / rename 前に停止した可能性。『未貼り付け』とは限らない。")
        elif delivered:
            print("packet は搬出済み (delivered)。席の受信・回答状況を確認し、同じ依頼を再送しないでください。")
        elif delivered is None:
            print("搬出履歴を確定できません。席の受信状況を確認し、同じ依頼を再送しないでください。")
        else:
            print("クリップボード搬出なし (--no-clipboard)。packet が席に届いていない可能性。")
    return 1


def _delivery_status(rec) -> bool | None:
    """選択経路と、実際に搬出成功を記録した履歴を区別する。"""
    if rec.get("delivery_confirmed") or rec["state"] in {
        "delivered", "output-received", "validated", "merged"
    }:
        return True
    if rec.get("delivery_route") == "stdout-only":
        return False
    return None


def _cmd_collect(args) -> int:
    """既存 invocation の scratch を回収する (dispatch --async の続き)。"""
    tp = ensure_topic(Path(args.root), args.slug)
    journal = Journal.load(tp)
    inv = args.invocation
    if inv not in journal.data["invocations"]:
        print(f"unknown invocation: {inv}", file=sys.stderr)
        return 2
    rec = journal.data["invocations"][inv]
    delivered = _delivery_status(rec)
    return _collect_one(tp, journal, inv, rec["participant"], args.timeout, delivered)


def _cmd_handoff(args) -> int:
    """同じinvocationの依頼を固定し、明示--apply時だけ選択した席へ搬出する。"""
    from . import handoff

    if not (topic_dir(Path(args.root), args.slug) / "minutes.md").is_file():
        print(json.dumps({"ok": False, "reason": "unknown-topic", "exit_code": 2}))
        return 2
    tp = ensure_topic(Path(args.root), args.slug)
    try:
        result = handoff.prepare(
            tp, args.invocation, transport=args.transport,
            workspace=args.workspace, surface=args.surface,
            script=Path(args.cmux_script) if args.cmux_script else None,
        )
        if args.apply and result.get("ok"):
            result = handoff.deliver(tp, args.invocation, timeout_s=args.timeout)
    except (ValueError, OSError) as exc:
        result = {"ok": False, "reason": "handoff-invalid", "detail": str(exc)}
    payload = {**result, "invocation": args.invocation,
               "exit_code": 0 if result.get("ok") else 1}
    _write_last_result(tp, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    return payload["exit_code"]


def _cmd_followup(args) -> int:
    """回収後の戻し先と通知証拠を固定する。外部送信はnative tool側が担う。"""
    from . import followup

    if not (topic_dir(Path(args.root), args.slug) / "minutes.md").is_file():
        print(json.dumps({"ok": False, "reason": "unknown-topic"}))
        return 2
    tp = ensure_topic(Path(args.root), args.slug)
    try:
        if args.action == "prepare":
            if not args.target_thread:
                raise ValueError("prepare requires --target-thread")
            result = followup.prepare(tp, args.invocation, args.target_thread)
        elif args.action == "claim":
            result = followup.claim(tp, args.invocation)
        elif args.action == "record":
            if not all((args.target_thread, args.message_sha256, args.receipt)):
                raise ValueError("record requires --target-thread, --message-sha256 and --receipt")
            receipt_path = Path(args.receipt)
            with receipt_path.open("rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError("receipt exceeds 64 KiB")
            receipt = json.loads(raw)
            result = followup.record_delivery(
                tp, args.invocation, args.target_thread, args.message_sha256, receipt,
            )
        else:
            result = followup.status(tp, args.invocation)
    except (ValueError, OSError, UnicodeError) as exc:
        result = {"ok": False, "reason": "followup-invalid", "detail": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result.get("ok") else 1


def _cmd_handoff_status(args) -> int:
    """搬出receiptをreadbackする。再送・再開はしない。"""
    from . import handoff

    if not (topic_dir(Path(args.root), args.slug) / "minutes.md").is_file():
        print(json.dumps({"ok": False, "reason": "unknown-topic", "exit_code": 2}))
        return 2
    tp = ensure_topic(Path(args.root), args.slug)
    try:
        result = handoff.status(tp, args.invocation)
    except (ValueError, OSError) as exc:
        result = {"ok": False, "reason": "handoff-invalid", "detail": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result.get("ok") else 1


def _cmd_cancel(args) -> int:
    """回収を取り消す。席の実行停止や外部メッセージ送信は行わない。"""
    if not (topic_dir(Path(args.root), args.slug) / "minutes.md").is_file():
        print(json.dumps({"ok": False, "reason": "unknown-topic", "exit_code": 2}))
        return 2
    tp = ensure_topic(Path(args.root), args.slug)
    try:
        result = watcher.cancel(tp, args.invocation)
    except ValueError as exc:
        result = {"ok": False, "reason": "invalid-invocation", "detail": str(exc)}
    payload = {**result, "invocation": args.invocation,
               "exit_code": 0 if result["ok"] else 1}
    _write_last_result(tp, payload)
    print(json.dumps(payload, ensure_ascii=False))
    return payload["exit_code"]


def _cmd_collect_pending(args) -> int:
    """永続journalから未回収を発見する有限poll。AIの新規起動・再送はしない。"""
    if not (topic_dir(Path(args.root), args.slug) / "minutes.md").is_file():
        print(json.dumps({"ok": False, "reason": "unknown-topic", "exit_code": 2}))
        return 2
    tp = ensure_topic(Path(args.root), args.slug)
    deadline = time.monotonic() + args.timeout
    attempted = set()
    results = {}
    while True:
        journal = Journal.load(tp)
        for inv, rec in journal.data["invocations"].items():
            if inv in attempted or rec["state"] in {"merged", "failed", "cancelled"}:
                continue
            if not any((tp.scratch / f"{inv}{suffix}").exists()
                       for suffix in (".json", ".json.tmp")) and not (
                           rec["state"] == "validated" and rec.get("response_sha256")
                       ):
                continue
            try:
                rc = _collect_one(tp, Journal.load(tp), inv, rec["participant"],
                                  0, delivered=_delivery_status(rec), lock_timeout_s=0)
            except LockTimeout:
                # 搬出中・他collector使用中の席は、ほかの回答を妨げず次回に回す。
                continue
            attempted.add(inv)
            results[inv] = {"exit_code": rc}
        journal = Journal.load(tp)
        pending = [inv for inv, rec in journal.data["invocations"].items()
                   if rec["state"] not in {"merged", "failed", "cancelled"}]
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(min(args.poll_interval, max(0, deadline - time.monotonic())))
        # 不完全tmpは次の観測で再検査できる。終端failedは自動再試行しない。
        attempted = {inv for inv in attempted
                     if journal.data["invocations"][inv]["state"] in {"merged", "failed", "cancelled"}}
    merged = [inv for inv, rec in journal.data["invocations"].items()
              if rec["state"] == "merged"]
    failures = [{"invocation": inv, "detail": rec["detail"]}
                for inv, rec in journal.data["invocations"].items()
                if rec["state"] == "failed"]
    cancelled = [inv for inv, rec in journal.data["invocations"].items()
                 if rec["state"] == "cancelled"]
    rc = 1 if failures else (2 if pending else 0)
    payload = {
        "ok": rc == 0, "reason": "errors" if failures else ("pending" if pending else "settled"),
        "exit_code": rc, "topic_path": str(tp.root.resolve()),
        "pending": pending, "merged": merged, "failed": failures,
        "cancelled": cancelled, "attempted": results,
        "follow_up": {
            "state": "review-required" if merged else "not-required",
            "invocations": merged,
            "resume_executed": False,
            "reason": "coordinator-resume-adapter-unavailable",
        },
    }
    # last-resultは最新表示。復旧の正本はgit検証済みjournal、mtimeに依存しない。
    _write_last_result(tp, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    return rc


def _nonnegative_seconds(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("有限の0以上の秒数が必要")
    return number


def _positive_seconds(value: str) -> float:
    number = _nonnegative_seconds(value)
    if number == 0:
        raise argparse.ArgumentTypeError("0より大きい秒数が必要")
    return number


def _cmd_status(args) -> int:
    """round / invocation / human_actions / failure_stats を表示する。"""
    tp = ensure_topic(Path(args.root), args.slug)
    journal = Journal.load(tp)
    print(f"round: {journal.round_no}")
    print(f"human_actions: {journal.human_action_count()}")
    stats = journal.failure_stats()
    if stats:
        parts = ", ".join(f"{k}={v}" for k, v in sorted(stats.items()))
        print(f"failure_stats: {parts}")
    else:
        print("failure_stats: (none)")
    conflicts = journal.conflicts()
    if conflicts:
        # 並行書き込みで解決できなかった記録。件数だけでも出さないと、
        # 「消えた」と「衝突した」の区別が CEO 側で永久につかない。
        print(f"write_conflicts: {len(conflicts)} 件 (journal.json の conflicts を参照)")
        for c in conflicts:
            print(
                f"  {c.get('invocation')}  kept={c.get('kept', {}).get('state')}  "
                f"dropped={c.get('dropped', {}).get('state')}  at={c.get('at')}"
            )
    seats = load_seats(tp)
    if seats:
        print("seats:")
        for key, seat in sorted(seats.items()):
            tier = seat.get("tier")
            line = f"  {key}  tier={tier}"
            if seat.get("fallback_reason"):
                line += f"  fallback_reason: {seat['fallback_reason']}"
            log = seat.get("permission_log")
            if log:
                line += f"  permissions: {len(log)} 件"
                if seat.get("permission_log_partial"):
                    line += " (turn 未完 — 全件とは限らない)"
            print(line)

    invocations = journal.data["invocations"]
    if not invocations:
        print("(invocation なし)")
        return 0
    print(f"{'invocation':<14} {'participant':<12} {'round':<6} {'state':<16} detail")
    for inv, rec in invocations.items():
        print(
            f"{inv:<14} {rec['participant']:<12} {rec['round']:<6} "
            f"{rec['state']:<16} {rec['detail']}"
        )
    return 0


def _cmd_close(args) -> int:
    """未解決一覧を必ず表示してから verdict を記入して close する (偽装成功防止)。"""
    tp = ensure_topic(Path(args.root), args.slug)
    journal = Journal.load(tp)
    unresolved = journal.unresolved()
    if unresolved:
        print(f"未解決一覧 ({len(unresolved)} 件) — merged に到達していない invocation:")
        for f in unresolved:
            print(
                f"  {f['invocation']}  {f['participant']}  round={f['round']}  "
                f"state={f['state']}  detail: {f['detail']}"
            )
    else:
        print("未解決なし (全 invocation が merged)。")
    minutes.write_verdict(tp, args.verdict)
    journal.record_human_action("close", args.verdict)
    print(f"closed: verdict: {args.verdict}")
    print(f"human_actions total: {journal.human_action_count()}")
    return 0


def _cmd_workflow_gate(args) -> int:
    """独立review成果物を開始／fan-in gateとして検査する。"""
    try:
        data = load_manifest(Path(args.manifest))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[workflow-gate failed] manifest を読めない: {exc}")
        return 1
    errors = validate_review_workflow(data, phase=args.phase)
    if not errors:
        errors.extend(validate_live_git(data, phase=args.phase, repo=Path(args.repo)))
    if errors:
        print("[workflow-gate failed]")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"[workflow-gate ok] phase={args.phase} workflow_id={data['workflow_id']}")
    return 0


def _cmd_doctor(args) -> int:
    """Tier1 可否を短時間診断する。議題なしでも可。"""
    from .doctor import format_report, run_doctor

    cwd = None
    if args.root:
        cwd = str(Path(args.root).resolve())
    report = run_doctor(
        binary=args.binary,
        probe_start=not args.skip_start,
        start_timeout=args.start_timeout,
        cwd=cwd,
    )
    print(format_report(report))
    if args.json:
        import json

        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=1))
    # doctor は診断専用。codex 不在でも exit 0 (推奨 tier を読めばよい)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="roundtable", description="人間司会のマルチ AI 壁打ち dispatcher")
    sub = parser.add_subparsers(dest="command", required=True)

    p_new = sub.add_parser("new-topic", help="議題を開始する")
    p_new.add_argument("slug")
    p_new.add_argument("--topic", required=True, help="議題タイトル")
    p_new.add_argument("--participants", required=True, help="参加者 (カンマ区切り)")
    p_new.add_argument("--background", default="", help="## 背景 に書く本文 (S1)")
    p_new.add_argument("--root", required=True, help="roundtable root ディレクトリ")
    p_new.set_defaults(func=_cmd_new_topic)

    p_bg = sub.add_parser("set-background", help="既存議題の背景を更新する")
    p_bg.add_argument("slug")
    p_bg.add_argument("--text", required=True)
    p_bg.add_argument("--root", required=True)
    p_bg.set_defaults(func=_cmd_set_background)

    p_dis = sub.add_parser("dispatch", help="指名 1 席分の packet を出して回収する")
    p_dis.add_argument("slug")
    p_dis.add_argument("--participant", required=True, help="指名する参加者")
    p_dis.add_argument("--role-hint", default="", help="参加者への役割ヒント")
    p_dis.add_argument("--no-clipboard", action="store_true", help="搬出を行わない (stdout のみ)")
    p_dis.add_argument("--timeout", type=_nonnegative_seconds, default=900.0, help="回収 timeout 秒")
    p_dis.add_argument("--tier", type=int, default=3, choices=[1, 3], help="relay tier (2 は未実装)")
    p_dis.add_argument(
        "--async",
        dest="async_dispatch",
        action="store_true",
        help="搬出だけして即 return。回収は collect で",
    )
    p_dis.add_argument("--root", required=True)
    p_dis.set_defaults(func=_cmd_dispatch)

    p_col = sub.add_parser("collect", help="既存 invocation を回収する")
    p_col.add_argument("slug")
    p_col.add_argument("--invocation", required=True)
    p_col.add_argument("--timeout", type=_nonnegative_seconds, default=900.0)
    p_col.add_argument("--root", required=True)
    p_col.set_defaults(func=_cmd_collect)

    p_ho = sub.add_parser("handoff", help="依頼を固定し、--apply時だけ既存席へ搬出する")
    p_ho.add_argument("slug")
    p_ho.add_argument("--root", required=True)
    p_ho.add_argument("--invocation", required=True)
    p_ho.add_argument("--transport", choices=("cmux", "claude-desktop"), required=True)
    p_ho.add_argument("--workspace", help="CMUX workspace UUID")
    p_ho.add_argument("--surface", help="CMUX surface UUID")
    p_ho.add_argument("--cmux-script", help="既存cmux_file_signal.pyの絶対パス")
    p_ho.add_argument("--apply", action="store_true", help="固定した依頼を選択済み搬送へ渡す")
    p_ho.add_argument("--timeout", type=_positive_seconds, default=30.0)
    p_ho.set_defaults(func=_cmd_handoff)

    p_hs = sub.add_parser("handoff-status", help="搬出receiptと回収状態を読む")
    p_hs.add_argument("slug")
    p_hs.add_argument("--root", required=True)
    p_hs.add_argument("--invocation", required=True)
    p_hs.set_defaults(func=_cmd_handoff_status)

    p_follow = sub.add_parser("followup", help="回収後の元担当への通知を固定・照合する")
    p_follow.add_argument("slug")
    p_follow.add_argument("--root", required=True)
    p_follow.add_argument("--invocation", required=True)
    p_follow.add_argument("--action", choices=("prepare", "claim", "record", "status"), default="status")
    p_follow.add_argument("--target-thread")
    p_follow.add_argument("--message-sha256")
    p_follow.add_argument("--receipt")
    p_follow.set_defaults(func=_cmd_followup)

    p_pending = sub.add_parser("collect-pending", help="journalから未回収を発見して有限待機で回収")
    p_pending.add_argument("slug")
    p_pending.add_argument("--root", required=True)
    p_pending.add_argument("--timeout", type=_nonnegative_seconds, default=0.0)
    p_pending.add_argument("--poll-interval", type=_positive_seconds, default=2.0)
    p_pending.set_defaults(func=_cmd_collect_pending)

    p_cancel = sub.add_parser("cancel", help="invocationの回収を取消（席の停止は別）")
    p_cancel.add_argument("slug")
    p_cancel.add_argument("--root", required=True)
    p_cancel.add_argument("--invocation", required=True)
    p_cancel.set_defaults(func=_cmd_cancel)

    p_st = sub.add_parser("status", help="round / invocation / KPI / 失敗統計を表示する")
    p_st.add_argument("slug")
    p_st.add_argument("--root", required=True)
    p_st.set_defaults(func=_cmd_status)

    p_cl = sub.add_parser("close", help="未解決一覧を表示してから verdict で close する")
    p_cl.add_argument("slug")
    p_cl.add_argument("--verdict", required=True, help="CEO の裁定")
    p_cl.add_argument("--root", required=True)
    p_cl.set_defaults(func=_cmd_close)

    p_doc = sub.add_parser("doctor", help="Codex Tier1 / 実席経路の環境診断")
    p_doc.add_argument("--binary", default=None, help="codex 実行ファイル")
    p_doc.add_argument("--root", default="", help="thread/start に渡す cwd (任意)")
    p_doc.add_argument("--skip-start", action="store_true", help="thread/start プローブを省略")
    p_doc.add_argument("--start-timeout", type=float, default=180.0, help="start プローブ秒")
    p_doc.add_argument("--json", action="store_true", help="JSON も追加出力")
    p_doc.set_defaults(func=_cmd_doctor)

    p_wf = sub.add_parser("workflow-gate", help="review-to-implementation manifest を検査する")
    p_wf.add_argument("manifest")
    p_wf.add_argument("--phase", choices=("start", "fan-in"), required=True)
    p_wf.add_argument("--repo", required=True)
    p_wf.set_defaults(func=_cmd_workflow_gate)

    return parser


def main(argv: list[str] | None = None) -> int:
    """entry point。テストから argv を直接渡して呼べる。"""
    args = _build_parser().parse_args(argv)
    try:
        return args.func(args)
    except LedgerDirtyError as exc:
        # fail-closed: 状態ファイル (journal / seats) や議事録が dispatcher 以外に
        # 書かれていたら、その先の記録は信用できない。握り潰さず CEO に提示して止める。
        # minutes.MinutesTamperedError もこの型なのでここに来る (exit 3)。
        # 裁定材料 (どのファイルがどう変わったか) は例外メッセージの diff にある (D12)。
        print(f"\n[tampered] 改ざん (dispatcher 以外の書込) を検知した\n{exc}", file=sys.stderr)
        return 3
    except LockTimeout as exc:
        # ロックを取れないまま書くと重ね合わせが不可分でなくなる (filelock の呼び出し規約:
        # 「呼び出し側は失敗として記録すること」)。traceback で落とさず分類済み失敗にする。
        print(f"\n[lock] 状態ファイルのロックを取得できなかった\n{exc}", file=sys.stderr)
        print("  別の dispatch が書き込み中か、ロックの残骸が残っている可能性がある。",
              file=sys.stderr)
        _try_write_last_result(
            args, {"ok": False, "reason": "lock", "detail": str(exc), "exit_code": 4}
        )
        return 4


def entrypoint() -> int:
    """プロセスのCLI入口は日本語・JSONをUTF-8で出力する。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    return main()


if __name__ == "__main__":
    sys.exit(entrypoint())
