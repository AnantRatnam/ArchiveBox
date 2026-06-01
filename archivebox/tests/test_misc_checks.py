import os
import signal

from archivebox.misc.checks import _migration_interrupt_message
from archivebox.misc.checks import _exit_on_migration_interrupt


def test_migration_interrupt_message_prints_resume_command_and_atomic_safety():
    message = _migration_interrupt_message()

    assert "Migration interrupted." in message
    assert "Database migrations are atomic" in message
    assert "no data loss has occurred" in message
    assert "archivebox init" in message


def test_migration_interrupt_message_before_apply_says_no_changes_applied():
    message = _migration_interrupt_message(before_apply=True)

    assert "cancelled before any changes were applied" in message
    assert "archivebox init" in message


def test_migration_interrupt_handler_exits_for_sigint_and_sigterm(monkeypatch):
    def fake_exit(code):
        raise SystemExit(code)

    monkeypatch.setattr("archivebox.misc.checks.os._exit", fake_exit)

    for sig in (signal.SIGINT, signal.SIGTERM):
        previous_handler = signal.getsignal(sig)
        try:
            with _exit_on_migration_interrupt():
                assert signal.getsignal(sig) != previous_handler
                os.kill(os.getpid(), sig)
        except SystemExit as err:
            assert err.code == 130
        else:
            raise AssertionError(f"{sig.name} should exit during migration auto-apply")
        assert signal.getsignal(sig) == previous_handler
