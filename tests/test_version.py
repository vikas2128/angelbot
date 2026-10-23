from bot import messages, version
from bot.api_server import code_check

A, B = "a" * 40, "b" * 40


def _repo(tmp_path, head="ref: refs/heads/main\n"):
    g = tmp_path / ".git"
    (g / "refs" / "heads").mkdir(parents=True)
    (g / "HEAD").write_text(head)
    return g


def test_head_reads_a_branch_a_packed_ref_and_a_detached_checkout(tmp_path):
    g = _repo(tmp_path)
    assert version.head(tmp_path) is None
    (g / "packed-refs").write_text(f"# pack-refs with: peeled\n{B} refs/heads/main\n")
    assert version.head(tmp_path) == B
    (g / "refs" / "heads" / "main").write_text(A + "\n")
    assert version.head(tmp_path) == A                    # a loose ref wins over the packed one
    (g / "HEAD").write_text(B + "\n")
    assert version.head(tmp_path) == B and version.short(B) == "bbbbbbb"
    assert version.head(tmp_path / "nowhere") is None


def test_code_check_flags_only_a_running_bot_or_api_on_older_code():
    assert code_check({"bot_alive": True, "code": A}, B, api_code=B) == {
        "checked_out": "bbbbbbb", "bot": "aaaaaaa", "api": "bbbbbbb", "bot_behind": True, "api_behind": False}
    assert not code_check({"bot_alive": False, "code": A}, B, api_code=B)["bot_behind"]      # a stopped bot reloads at start
    assert not code_check({"bot_alive": True, "code": None}, B, api_code=B)["bot_behind"]    # unknown: no warning
    assert not code_check({"bot_alive": True, "code": A}, None, api_code=A)["bot_behind"]
    assert code_check({"bot_alive": True, "code": B}, B, api_code=A)["api_behind"]


def test_telegram_status_warns_when_the_bot_runs_older_code(monkeypatch):
    monkeypatch.setattr(version, "head", lambda root=None: B)
    s = {"written_at": "x", "bot_alive": True, "mode": "paper", "code": A, "positions": []}
    assert "older code (aaaaaaa)" in messages.status_card(s, {})
    assert "older code" not in messages.status_card(dict(s, code=B), {})
