"""ai-roundtable — 人間司会のマルチ AI 壁打ち dispatcher (AI を実行しない)。"""

# 版番号の実行時正本。pyproject.toml の version と一致していることを
# tests/test_version_consistency.py が機械検査する (数字を 1 箇所だけ直して
# 席へ名乗る版がずれる事故を防ぐため)。
#
# importlib.metadata を使わないのは、この repo が install せず source から動く前提
# だから (CI も pip install -e . をしない)。未 install 環境では metadata が引けない。
__version__ = "0.3.0"
