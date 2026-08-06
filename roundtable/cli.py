"""roundtable CLI — new-topic / dispatch / status / close。

`python -m roundtable.cli <cmd>` で起動する。dispatch は AI を実行しない:
packet を生成して席へ配達し、scratch の出力を待つだけ。

配達経路は seats.json の tier で決まる (DESIGN v6 §4):
    Tier1  relay adapter が席のチャットへ届ける (人間の操作 0 回)
    Tier3  クリップボードへ搬出し、貼り付けは人間 (CEO) が行う (人間の操作 1 回)
Tier1 が失敗したら自動で Tier3 へ縮退し、その旨を表示する。**Tier2 (UI 自動化) へ
勝手に昇格しない** — 昇格には席単位の CEO 明示承認が要る (DESIGN v6 §4/§12)。
"""
import argparse
import sys
from pathlib import Path

from . import minutes, packet, watcher
from .journal import Journal
from .paths import ensure_topic
from .relay import RelayError, get_relay
from .seats import Seats


def _cmd_new_topic(args) -> int:
    """議題を開始する: ディレクトリ規約を作り minutes と席台帳を生成する。"""
    root = Path(args.root)
    tp = ensure_topic(root, args.slug)
    participants = [p.strip() for p in args.participants.split(",") if p.strip()]
    minutes.create(tp, args.topic, participants)
    ledger = Seats.load(root)
    for p in participants:
        ledger.ensure(args.slug, p, tier=args.tier)
    Journal.load(tp).record_human_action("topic")  # 議題宣言 = 人間の操作 1 回
    print(f"topic 作成: {tp.minutes}")
    print(f"participants: {', '.join(participants)}")
    print("席: " + _seat_summary(ledger, args.slug))
    return 0


def _seat_summary(ledger: Seats, slug: str) -> str:
    seats = ledger.for_topic(slug)
    if not seats:
        return "(未登録)"
    return ", ".join(f"{p}=tier{s['tier']}" for p, s in sorted(seats.items()))


def _deliver_tier3(seat: dict, text: str, degraded: bool) -> dict:
    """Tier3 (人間 relay): クリップボードへ搬出する。貼り付けは CEO。"""
    try:
        get_relay(3, seat["participant"]).send(seat, text)
    except RelayError as e:
        print(f"\n[deliver-failed] クリップボード搬出に失敗: {e}")
        print("上の packet 本文を手でコピーして席に貼り付けてください。")
        return {"tier": 3, "delivered": False, "human": 0, "relay": None, "degraded": degraded}
    print("\n[clipboard] packet をクリップボードに載せた。席のチャットに貼り付けてください。")
    # Tier3 の貼り付けは人間がやる → 1 回として計上する。Tier1 なら 0 に落ちる
    # (この差分が軸 A の効果測定そのもの)。
    return {"tier": 3, "delivered": True, "human": 1, "relay": None, "degraded": degraded}


def _deliver(ledger: Seats, seat: dict, text: str, root: Path) -> dict:
    """seat の tier に従って packet を配達する。Tier1 失敗時は Tier3 へ自動縮退。

    戻り値:
        tier       実際に配達に使った tier (縮退後の値)
        delivered  席への搬出が成立したか (Tier3 の delivered は「搬出済み」の意味)
        human      人間の操作として数える回数
        relay      **呼び出し側が close する責務を持つ** Relay (なければ None)。
                   Tier1 は席が作業している間プロセスを生かす必要があるため、
                   send 直後に close してはいけない (turn ごと切れる)。
        degraded   Tier1/2 → Tier3 の縮退が起きたか
    """
    tier = seat["tier"]
    if tier == 3:
        return _deliver_tier3(seat, text, degraded=False)

    relay = None
    try:
        relay = get_relay(tier, seat["participant"], cwd=root)
        relay.send(seat, text)
    except (RelayError, NotImplementedError, OSError) as e:
        if relay is not None:
            relay.close()
        print(f"\n[degrade] Tier{tier} relay が失敗したので Tier3 (人間 relay) に縮退する: {e}")
        print("(Tier2 (UI 自動化) へは昇格しない — 席単位の CEO 明示承認が要る)")
        return _deliver_tier3(seat, text, degraded=True)

    thread_ref = getattr(relay, "thread_ref", None)
    if thread_ref:
        ledger.record_thread(seat["topic"], seat["participant"], thread_ref)
    print(f"\n[tier{tier}] 席へ packet を送信した (thread: {thread_ref})。貼り付けは不要。")
    return {"tier": tier, "delivered": True, "human": 0, "relay": relay, "degraded": False}


def _delivery_detail(delivery: dict) -> str:
    """journal に残す配達経路の記録 (縮退したこと自体を証跡に残す)。"""
    if delivery["degraded"]:
        return f"tier{delivery['tier']} (tier1/2 から縮退)"
    return f"tier{delivery['tier']}"


def _cmd_dispatch(args) -> int:
    """指名 1 席分の packet を出し、配達 → collect (回収・検証・merge) まで実行する。"""
    root = Path(args.root)
    tp = ensure_topic(root, args.slug)
    minutes.make_snapshot(tp)  # 参加者に渡す読み取り用スナップショット (base_hash の基準)
    journal = Journal.load(tp)
    journal.record_human_action("nominate")  # dispatch の起動そのものが人間の指名操作
    inv = journal.new_invocation(args.participant, journal.round_no)

    text = packet.build(tp, args.participant, inv, role_hint=args.role_hint)
    print(text)

    ledger = Seats.load(root)
    seat = ledger.ensure(args.slug, args.participant, tier=args.tier)

    if args.no_deliver:
        print("\n[no-deliver] 配達しない (packet を表示しただけ)。")
        delivery = {
            "tier": seat["tier"], "delivered": False, "human": 0,
            "relay": None, "degraded": False,
        }
    else:
        delivery = _deliver(ledger, seat, text, root)

    if delivery["delivered"]:
        journal.set_state(inv, "delivered", _delivery_detail(delivery))
    for _ in range(delivery["human"]):
        journal.record_human_action("paste")

    try:
        result = watcher.collect(tp, journal, inv, args.participant, timeout_s=args.timeout)
    finally:
        # Tier1 は席が作業している間 relay を生かしておく必要がある。回収が終わって
        # (成功でも失敗でも) 初めて撤収する。孤児プロセスを残さないため finally。
        if delivery["relay"] is not None:
            delivery["relay"].close()

    if result["ok"]:
        journal.advance_round_if_complete(minutes.parse_participants(tp))
        minutes.sync_round(tp, journal.round_no)
        print(f"\n[ok] merge 完了 (invocation: {inv}) / round: {journal.round_no}")
        return 0

    reason = result["reason"]
    print(f"\n[failed] invocation: {inv} / reason: {reason}")
    if reason == "timeout":
        if not delivery["delivered"]:
            print("配達なし (--no-deliver または搬出失敗)。packet が席に届いていない可能性。")
        elif delivery["tier"] == 3:
            print("packet は搬出済み (delivered)。席に貼り付けたか確認してください (未貼り付け?)。")
        else:
            print("Tier1 で送信済み。席が出力を書かなかった (席側のチャットを確認してください)。")
    return 1


def _cmd_status(args) -> int:
    """round・KPI (人間の操作回数)・失敗分類・各 invocation の状態を表示する。

    status は観測専用なので human_actions には数えない (数えると観測が KPI を汚す)。
    """
    root = Path(args.root)
    tp = ensure_topic(root, args.slug)
    journal = Journal.load(tp)
    print(f"round: {journal.round_no}")
    print("席: " + _seat_summary(Seats.load(root), args.slug))

    counts = journal.human_action_counts
    breakdown = ", ".join(f"{k}: {v}" for k, v in counts.items())
    print(f"人間の操作: {journal.human_actions} 回" + (f" ({breakdown})" if breakdown else ""))

    failures = journal.failure_counts()
    if failures:
        print("失敗分類: " + ", ".join(f"{k}: {v}" for k, v in failures.items()))

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
    failures = journal.failure_counts()
    if failures:
        print("失敗分類: " + ", ".join(f"{k}: {v}" for k, v in failures.items()))
    minutes.write_verdict(tp, args.verdict)
    journal.record_human_action("verdict")  # 裁定 = 人間の操作 1 回
    print(f"closed: verdict: {args.verdict}")
    print(f"人間の操作 (この議題 合計): {journal.human_actions} 回")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="roundtable", description="人間司会のマルチ AI 壁打ち dispatcher")
    sub = parser.add_subparsers(dest="command", required=True)

    p_new = sub.add_parser("new-topic", help="議題を開始する")
    p_new.add_argument("slug")
    p_new.add_argument("--topic", required=True, help="議題タイトル")
    p_new.add_argument("--participants", required=True, help="参加者 (カンマ区切り)")
    p_new.add_argument("--root", required=True, help="roundtable root ディレクトリ")
    p_new.add_argument(
        "--tier", type=int, choices=(1, 2, 3), default=None,
        help="席の tier を seats.json に記録する (既定: 3 = 人間 relay)",
    )
    p_new.set_defaults(func=_cmd_new_topic)

    p_dis = sub.add_parser("dispatch", help="指名 1 席分の packet を出して回収する")
    p_dis.add_argument("slug")
    p_dis.add_argument("--participant", required=True, help="指名する参加者")
    p_dis.add_argument("--role-hint", default="", help="参加者への役割ヒント")
    p_dis.add_argument(
        "--no-deliver", "--no-clipboard", dest="no_deliver", action="store_true",
        help="配達しない (packet を表示するだけ)。--no-clipboard は旧名の別名",
    )
    p_dis.add_argument(
        "--tier", type=int, choices=(1, 2, 3), default=None,
        help="この席の tier を指定して seats.json に記録する (既定: 記録済みの値 or 3)",
    )
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
