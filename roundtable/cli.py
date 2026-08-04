"""roundtable CLI — new-topic / dispatch / status / close。

`python -m roundtable.cli <cmd>` で起動する。dispatch は AI を実行しない:
packet を生成してクリップボード (Tier3) に載せ、scratch の出力を待つだけ。
貼り付け先の chat 操作は人間 (CEO) が行う (DESIGN v6 §0)。
"""
import argparse
import sys
from pathlib import Path

from . import minutes, packet, watcher
from .journal import Journal
from .paths import ensure_topic


def _cmd_new_topic(args) -> int:
    """議題を開始する: ディレクトリ規約を作り minutes を生成する。"""
    tp = ensure_topic(Path(args.root), args.slug)
    participants = [p.strip() for p in args.participants.split(",") if p.strip()]
    minutes.create(tp, args.topic, participants)
    print(f"topic 作成: {tp.minutes}")
    print(f"participants: {', '.join(participants)}")
    return 0


def _cmd_dispatch(args) -> int:
    """指名 1 席分の packet を出し、collect (回収・検証・merge) まで実行する。"""
    tp = ensure_topic(Path(args.root), args.slug)
    minutes.make_snapshot(tp)  # 参加者に渡す読み取り用スナップショット (base_hash の基準)
    journal = Journal.load(tp)
    inv = journal.new_invocation(args.participant, journal.round_no)

    text = packet.build(tp, args.participant, inv, role_hint=args.role_hint)
    print(text)

    delivered = False
    if not args.no_clipboard:
        packet.to_clipboard(text)
        journal.set_state(inv, "delivered", "tier3")  # 搬出済み (席への着信は未知)
        delivered = True
        print("\n[clipboard] packet をクリップボードに載せた。席のチャットに貼り付けてください。")

    result = watcher.collect(tp, journal, inv, args.participant, timeout_s=args.timeout)

    if result["ok"]:
        journal.advance_round_if_complete(minutes.parse_participants(tp))
        minutes.sync_round(tp, journal.round_no)
        print(f"\n[ok] merge 完了 (invocation: {inv}) / round: {journal.round_no}")
        return 0

    reason = result["reason"]
    print(f"\n[failed] invocation: {inv} / reason: {reason}")
    if reason == "timeout":
        if delivered:
            print("packet は搬出済み (delivered)。席に貼り付けたか確認してください (未貼り付け?)。")
        else:
            print("クリップボード搬出なし (--no-clipboard)。packet が席に届いていない可能性。")
    return 1


def _cmd_status(args) -> int:
    """round と各 invocation の状態を表形式で表示する。"""
    tp = ensure_topic(Path(args.root), args.slug)
    journal = Journal.load(tp)
    print(f"round: {journal.round_no}")
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
    """未解決一覧を必ず表示してから verdict を記入して close する (偽装成功防止)。

    failed だけでなく merged 未到達の全状態を見せる (レビュー M3):
    Ctrl+C・clip 失敗・クラッシュで途中状態に残った invocation を「失敗なし」と
    誤認させない。
    """
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
    print(f"closed: verdict: {args.verdict}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="roundtable", description="人間司会のマルチ AI 壁打ち dispatcher")
    sub = parser.add_subparsers(dest="command", required=True)

    p_new = sub.add_parser("new-topic", help="議題を開始する")
    p_new.add_argument("slug")
    p_new.add_argument("--topic", required=True, help="議題タイトル")
    p_new.add_argument("--participants", required=True, help="参加者 (カンマ区切り)")
    p_new.add_argument("--root", required=True, help="roundtable root ディレクトリ")
    p_new.set_defaults(func=_cmd_new_topic)

    p_dis = sub.add_parser("dispatch", help="指名 1 席分の packet を出して回収する")
    p_dis.add_argument("slug")
    p_dis.add_argument("--participant", required=True, help="指名する参加者")
    p_dis.add_argument("--role-hint", default="", help="参加者への役割ヒント")
    p_dis.add_argument("--no-clipboard", action="store_true", help="クリップボード搬出を行わない")
    p_dis.add_argument("--timeout", type=float, default=900.0, help="回収 timeout 秒")
    p_dis.add_argument("--root", required=True)
    p_dis.set_defaults(func=_cmd_dispatch)

    p_st = sub.add_parser("status", help="round / invocation 状態を表示する")
    p_st.add_argument("slug")
    p_st.add_argument("--root", required=True)
    p_st.set_defaults(func=_cmd_status)

    p_cl = sub.add_parser("close", help="失敗一覧を表示してから verdict で close する")
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
