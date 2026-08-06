"""roundtable CLI — new-topic / dispatch / collect / status / close。

`python -m roundtable.cli <cmd>` で起動する。dispatch は AI を実行しない:
packet を生成して relay (既定 Tier3 クリップボード / 任意 Tier1) に載せ、
scratch の出力を待つだけ。貼り付け先の chat 操作は人間 (CEO) が行う
(DESIGN v6 §0)。Tier1 障害時は Tier3 に縮退し、Tier2 へは昇格しない。
"""
import argparse
import json
import sys
from pathlib import Path

from . import minutes, packet, watcher
from .journal import Journal
from .paths import ensure_topic
from .relay import get_relay, load_seats, save_seats


def _write_last_result(tp, payload: dict) -> None:
    minutes.atomic_write(tp.last_result, json.dumps(payload, ensure_ascii=False, indent=1))


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


def _cmd_dispatch(args) -> int:
    """指名 1 席分の packet を出し、既定では collect まで実行する。"""
    tp = ensure_topic(Path(args.root), args.slug)
    minutes.make_snapshot(tp)
    journal = Journal.load(tp)
    inv = journal.new_invocation(args.participant, journal.round_no)
    journal.record_human_action("dispatch", f"{args.participant}:{inv}")

    text = packet.build(tp, args.participant, inv, role_hint=args.role_hint)
    print(text)

    tier = args.tier
    if args.no_clipboard and tier == 3:
        # 明示的に搬出せず、packet を stdout のみ
        delivered = False
        relay_label = "stdout-only"
    else:
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
        relay = get_relay(args.participant, tier=tier, allow_fallback=True)
        try:
            relay_label = relay.send(seat, text)
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


def _collect_one(tp, journal, inv, participant, timeout_s, delivered: bool) -> int:
    result = watcher.collect(tp, journal, inv, participant, timeout_s=timeout_s)
    if result["ok"]:
        journal.advance_round_if_complete(minutes.parse_participants(tp))
        minutes.sync_round(tp, journal.round_no)
        _write_last_result(
            tp,
            {
                "ok": True,
                "reason": "merged",
                "invocation": inv,
                "exit_code": 0,
                "round": journal.round_no,
            },
        )
        print(f"\n[ok] merge 完了 (invocation: {inv}) / round: {journal.round_no}")
        return 0

    reason = result["reason"]
    _write_last_result(
        tp,
        {
            "ok": False,
            "reason": reason,
            "invocation": inv,
            "exit_code": 1,
        },
    )
    print(f"\n[failed] invocation: {inv} / reason: {reason}")
    if reason == "timeout":
        if delivered:
            print("packet は搬出済み (delivered)。席に貼り付けたか確認してください (未貼り付け?)。")
        else:
            print("クリップボード搬出なし (--no-clipboard)。packet が席に届いていない可能性。")
    return 1


def _cmd_collect(args) -> int:
    """既存 invocation の scratch を回収する (dispatch --async の続き)。"""
    tp = ensure_topic(Path(args.root), args.slug)
    journal = Journal.load(tp)
    inv = args.invocation
    if inv not in journal.data["invocations"]:
        print(f"unknown invocation: {inv}", file=sys.stderr)
        return 2
    rec = journal.data["invocations"][inv]
    delivered = rec["state"] in {"delivered", "output-received", "validated", "merged"}
    return _collect_one(tp, journal, inv, rec["participant"], args.timeout, delivered)


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
    p_dis.add_argument("--timeout", type=float, default=900.0, help="回収 timeout 秒")
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
    p_col.add_argument("--timeout", type=float, default=900.0)
    p_col.add_argument("--root", required=True)
    p_col.set_defaults(func=_cmd_collect)

    p_st = sub.add_parser("status", help="round / invocation / KPI / 失敗統計を表示する")
    p_st.add_argument("slug")
    p_st.add_argument("--root", required=True)
    p_st.set_defaults(func=_cmd_status)

    p_cl = sub.add_parser("close", help="未解決一覧を表示してから verdict で close する")
    p_cl.add_argument("slug")
    p_cl.add_argument("--verdict", required=True, help="CEO の裁定")
    p_cl.add_argument("--root", required=True)
    p_cl.set_defaults(func=_cmd_close)

    return parser


def main(argv: list[str] | None = None) -> int:
    """entry point。テストから argv を直接渡して呼べる。"""
    args = _build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
