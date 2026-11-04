# 指定議題の読取り契約

`roundtable.read_model.export_topic(root, slug)` は、既存の指定議題だけをJSON互換の辞書へ投影する。全会話の一覧や会話原本は取得しない。`topic_dir` のslug契約、既存のseatsとjournalの形、journalの状態名を利用する。ロックを作る既存loaderや `ensure_topic` は呼ばない。

schema_versionは1。observed_atはUTCのISO形式。statusはavailableまたはunknown。reasonは正常時null、不足・非対応・読取り失敗時に理由を返す。topicはslugとround、minutesは議事録の文字列。必須のjournal.json、minutes.mdが欠ける場合はunknownとし、空の投影を返す。

seatsはid、provider（codex、claude、other）、conversation_id、title、state、receiptの配列。conversation_idはthread_refのJSON形を保つ。titleは既存title、thread_name、席キーの順で取得する。stateは常にunknownであり、議事録や送達記録から稼働状態を推測しない。

invocationsはid、participant、round、state、receiptの配列。stateはjournalの記録であり、モデルの現在状態ではない。seats.jsonが未作成なら空席として扱い、既存Claude搬出記録のclaude_sessionから指定席を投影する。invocationのsubmission_receiptは既存Codexのprovider、thread_id、turn_id、statusだけを保持する。receiptは席のsubmission_receiptまたは既存deliveriesのstate、transport、delivery_route、model_receipt_verifiedだけを表示する。送信本文、socket、会話ファイルの参照先を出力しない。送達・回答回収・人間採用を同じ完了状態に変換しない。

読取りはファイルごと8 MiBに制限し、symlinkを拒否する。主要3ファイルの再読込みが異なる場合はunknown。同一時点のトランザクション、Gitによる改ざん検証、liveセッションの確認を保証しない。権限不足、途中書込み、古い形式はunknownとして扱う。表示からsend、collect、watch、Git初期化、台帳・設定の更新を起動しない。

通常ファイルだけを有界で読む。POSIXでは各親をディレクトリFDで固定し、リンクを開かない。Windowsでは再解析ポイントとファイルの同一性を前後で検査する。Windows固有の競合耐性は実機受入で確認する。

検証は人工fixtureだけで行う。実席・配布アプリ・本人採用の確認は別に残る。

## 共通画面の起動

```bash
ai-roundtable export selected-topic --root /path/to/minutes-root
ai-roundtable gui selected-topic --root /path/to/minutes-root
# Safe側で事前出力したJSONを任意で表示する
ai-roundtable gui selected-topic --root /path/to/minutes-root --safe-export /path/to/safe-export.json
```

GUIはPython標準のTkを使う。Tk非対応のPythonではexportを利用する。画面は指定議題だけを再読込みし、台帳へ書かない。SafeのJSONは指定ファイルの固定した観測であり、現在の接続や送信可能性を表さない。再読込みは台帳を更新しない。送信・回収・採否は独立した操作で、自動再開はSafe Resumeが管理する。共通画面への表示が、既存CLIの信頼検査を代行することはない。

## 円卓の操作

共通画面は議題を読む。操作は「依頼を準備」「送信内容を確認」「回答を回収」「座長の採否」から明示して行う。準備は既存dispatcherのstdout-onlyで、依頼と議事録の固定版を保存する。送信確認では既存会話UUIDと固定版を並べる。Codexは登録済み席の一致を検査して既存App Serverで搬送し、最大300秒の回収を試みる。期限後にその接続を閉じるため長い回答が中断される場合がある。Claudeは指定済みDesktop席の既存socket搬送と回収を使用する。WindowsのClaude socket搬送は未対応。

送達不明は再送を止め、依頼IDで既存回収経路を確認する。座長の採否は回答が一件以上回収されてから、判断と理由を既存closeで議事録に書く。未回収件数も判断時に表示する。

「反証」「振り返り」は既存の依頼に観点を加える。準備、確認、送信、回収、座長判断は通常依頼と同じ。Codexの宛先は登録済み席に限る。Windowsでの同時書込みと表示の競合は実機検証項目。
