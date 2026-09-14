from __future__ import annotations

import datetime as dt
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import open_microsoft_email as app


def test_id_zero_monitor_clicks_once_per_visible_appearance() -> None:
    states = iter((False, True, True, False, False, True))
    probe_done = threading.Event()
    clicks: list[str] = []

    def probe(page: object) -> bool:
        try:
            return next(states)
        except StopIteration:
            probe_done.set()
            return False

    monitor = app.IdZeroMonitor(
        object(),
        probe=probe,
        clicker=lambda page: clicks.append("id__0"),
        poll_interval=0.001,
    )
    monitor.start()
    assert probe_done.wait(1.0)
    monitor.stop()

    assert clicks == ["id__0", "id__0"]
    assert not monitor.is_alive


def test_id_zero_monitor_survives_probe_error_and_stops() -> None:
    calls = 0
    ready = threading.Event()

    def probe(page: object) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient page navigation")
        ready.set()
        return False

    monitor = app.IdZeroMonitor(
        object(),
        probe=probe,
        poll_interval=0.001,
    )
    monitor.start()
    assert ready.wait(1.0)
    monitor.stop()
    monitor.stop()

    assert calls >= 2
    assert not monitor.is_alive


def test_probe_and_click_id_zero_uses_atomic_page_script() -> None:
    scripts: list[str] = []

    class FakePage:
        def run_js(self, script: str, *, timeout: float) -> dict[str, object]:
            scripts.append(script)
            return {"present": True, "clicked": True}

    assert app.probe_and_click_id_zero(FakePage()) is True
    assert scripts and "#id__0" in scripts[0]


def test_button_monitor_checks_ok_id_and_yes_primary_button() -> None:
    scripts: list[str] = []

    class FakePage:
        def run_js(self, script: str, *, timeout: float) -> dict[str, object]:
            scripts.append(script)
            return {"present": True, "clicked": True}

    assert app.probe_and_click_id_zero(FakePage()) is True
    assert scripts
    assert "#id__0" in scripts[0]
    assert "data-testid=\"primaryButton\"" in scripts[0]
    assert "Yes" in scripts[0]


def test_button_monitor_checks_disabled_state_on_click_target() -> None:
    script = app.ID_ZERO_PROBE_CLICK_JS

    # ``#id__0`` is the nested label span in Microsoft's OK button.  The
    # disabled state belongs to its closest button, not necessarily the span.
    assert "const clickTarget = element.closest" in script
    assert "clickTarget.disabled" in script
    assert "clickTarget.getAttribute('aria-disabled')" in script
    assert script.index("const clickTarget = element.closest") < script.index(
        "clickTarget.disabled"
    )


def test_button_monitor_default_poll_interval_is_one_second() -> None:
    assert app.ID_ZERO_POLL_INTERVAL == 1.0


def test_id_zero_monitor_follows_tabs_added_after_start() -> None:
    tab_added = threading.Event()
    tab_clicked = threading.Event()

    class FakePage:
        def __init__(self) -> None:
            self.tabs: list[object] = []

    page = FakePage()
    tab = object()

    def probe(target: object) -> bool:
        if target is tab:
            tab_clicked.set()
            return True
        return False

    def clicker(target: object) -> None:
        assert target is tab

    monitor = app.IdZeroMonitor(
        page,
        probe=probe,
        clicker=clicker,
        poll_interval=0.001,
    )
    monitor.start()
    page.tabs.append(tab)
    tab_added.set()
    assert tab_added.is_set()
    assert tab_clicked.wait(1.0)
    monitor.stop()

    assert monitor.click_count == 1
    assert not monitor.is_alive


def test_id_zero_monitor_deduplicates_wrappers_for_same_context() -> None:
    clicked = threading.Event()
    click_count = 0

    class Wrapper:
        _context_id = "context-1"

    class FakePage:
        _context_id = "root"

        def get_tabs(self) -> list[Wrapper]:
            # RuyiPage may create a fresh wrapper object on every refresh.
            return [Wrapper()]

    def probe(target: object) -> bool:
        return isinstance(target, Wrapper)

    def clicker(target: object) -> None:
        nonlocal click_count
        click_count += 1
        clicked.set()

    monitor = app.IdZeroMonitor(
        FakePage(),
        probe=probe,
        clicker=clicker,
        poll_interval=0.001,
    )
    monitor.start()
    assert clicked.wait(1.0)
    time.sleep(0.02)
    monitor.stop()

    assert click_count == 1


def test_id_zero_monitor_deduplicates_wrappers_by_tab_id() -> None:
    """Wrappers exposing only RuyiPage's public tab_id remain one target."""
    clicked = threading.Event()
    click_count = 0

    class Wrapper:
        tab_id = "tab-public-1"

    class FakePage:
        tab_id = "root-public"

        def __init__(self) -> None:
            self.created: list[Wrapper] = []

        def get_tabs(self) -> list[Wrapper]:
            # A fresh wrapper is returned on every scan, as RuyiPage does.
            wrapper = Wrapper()
            self.created.append(wrapper)
            return [wrapper]

    def probe(target: object) -> bool:
        return isinstance(target, Wrapper)

    def clicker(target: object) -> None:
        nonlocal click_count
        click_count += 1
        clicked.set()

    page = FakePage()
    monitor = app.IdZeroMonitor(
        page,
        probe=probe,
        clicker=clicker,
        poll_interval=0.001,
    )
    monitor.start()
    assert clicked.wait(1.0)
    time.sleep(0.02)
    monitor.stop()

    assert click_count == 1


def test_id_zero_monitor_can_restart_after_stop_before_start() -> None:
    calls = 0
    ready = threading.Event()

    def probe(page: object) -> bool:
        nonlocal calls
        calls += 1
        ready.set()
        return False

    monitor = app.IdZeroMonitor(object(), probe=probe, poll_interval=0.001)
    monitor.stop()
    monitor.start()
    assert ready.wait(1.0)
    monitor.stop()

    assert calls >= 1
    assert not monitor.is_alive


def test_open_email_page_starts_and_stops_id_zero_monitor_after_enter(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class FakeCore:
        @staticmethod
        def launch_ruyi_browser(proxy: str, **kwargs: object) -> object:
            events.append("launch")
            return object()

        @staticmethod
        def navigate_with_retry(page: object, url: str, description: str, **kwargs: object) -> dict:
            events.append("navigate")
            return {"href": url}

        @staticmethod
        def close_ruyi_browser(page: object, **kwargs: object) -> None:
            events.append("close")

    monitor = object()

    def monitor_factory(page: object) -> object:
        events.append("monitor-start")
        return monitor

    def monitor_stopper(value: object, *, timeout: float) -> None:
        assert value is monitor
        events.append("monitor-stop")

    result = app.open_email_page(
        app.OpenOptions(cache_dir=tmp_path / "cache", snapshot_dir=tmp_path / "snapshots"),
        core_module=FakeCore,
        input_fn=lambda prompt: events.append("enter") or "",
        monitor_factory=monitor_factory,
        monitor_stopper=monitor_stopper,
    )

    assert result == 0
    assert events == ["launch", "monitor-start", "navigate", "enter", "monitor-stop", "close"]


def test_open_email_page_skips_default_monitor_without_run_js(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monitor_calls: list[object] = []

    class Page:
        def ele(self, selector: str, timeout: float = 0.1) -> object | None:
            return None

    class FakeCore:
        @staticmethod
        def launch_ruyi_browser(proxy: str, **kwargs: object) -> Page:
            return Page()

        @staticmethod
        def navigate_with_retry(page: object, url: str, description: str, **kwargs: object) -> dict:
            return {"href": url}

        @staticmethod
        def close_ruyi_browser(page: object, **kwargs: object) -> None:
            return None

    monkeypatch.setattr(
        app,
        "start_id_zero_monitor",
        lambda page: monitor_calls.append(page),
    )

    result = app.open_email_page(
        app.OpenOptions(cache_dir=tmp_path / "cache", snapshot_dir=tmp_path / "snapshots"),
        core_module=FakeCore,
        input_fn=lambda prompt: "",
    )

    assert result == 0
    assert monitor_calls == []


def test_parse_defaults() -> None:
    args = app.parse_args([])
    assert args.url == app.DEFAULT_URL
    assert args.keep_open is True
    assert args.headless is False


def test_options_validate_and_resolve(tmp_path: Path) -> None:
    args = app.parse_args(
        [
            "--url",
            "https://outlook.live.com/mail/",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--no-keep-open",
            "--timeout",
            "12",
        ]
    )
    options = app.options_from_args(args)
    assert options.url.endswith("/mail/")
    assert options.cache_dir == (tmp_path / "cache").resolve()
    assert options.keep_open is False
    assert options.timeout == 12


def test_options_reject_bad_url() -> None:
    with pytest.raises(ValueError, match="--url"):
        app.options_from_args(SimpleNamespace(
            url="file:///tmp/page", cache_dir="cache", snapshot_dir="snap",
            timeout=1, headless=False, keep_open=False, clear_cache=False,
        ))


def test_read_credentials_parses_single_complete_line() -> None:
    prompts: list[str] = []

    def input_fn(prompt: str) -> str:
        prompts.append(prompt)
        return "person@example.com----secret----client-123----token-456"

    credentials = app.read_credentials(input_fn=input_fn)
    assert credentials.email == "person@example.com"
    assert credentials.password == "secret"
    assert credentials.client_id == "client-123"
    assert credentials.token == "token-456"
    assert prompts == ["请输入邮箱----密码----client_id----令牌: "]


def test_read_credentials_rejects_incomplete_line() -> None:
    with pytest.raises(ValueError, match="四段"):
        app.read_credentials(input_fn=lambda prompt: "person@example.com----secret")


def test_parse_credential_line_and_extract_security_code() -> None:
    credentials = app.parse_credential_line(
        "person@example.com----secret----client-123----token-456"
    )
    assert credentials == app.AccountCredentials(
        email="person@example.com",
        password="secret",
        client_id="client-123",
        token="token-456",
    )
    body = "<div>Security code: <strong>524719</strong></div>"
    assert app.extract_security_code(body) == "524719"
    assert app.extract_security_code("Security code: **524719**") == "524719"


def test_fill_security_code_accepts_numeric_result() -> None:
    events: list[tuple[str, str]] = []
    intervals: list[float] = []

    class Element:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        def input(self, value: str, clear: bool = False) -> None:
            events.append((self.selector, value))

    def waiter(page: object, selector: str, description: str, timeout: float) -> Element:
        return Element(selector)

    def clicker(page: object, selector: str, description: str, timeout: float) -> None:
        events.append((selector, "click"))

    app.fill_security_code(
        object(),
        524719,
        timeout=1,
        waiter=waiter,
        clicker=clicker,
        sleeper=intervals.append,
    )
    assert [value for selector, value in events if selector.startswith("#codeEntry-")] == list("524719")
    assert intervals == [app.SECURITY_CODE_INPUT_INTERVAL] * 5


def test_fill_security_code_keeps_first_digit_when_otp_autofocuses() -> None:
    values = [""] * 6
    clear_flags: list[bool] = []

    class Element:
        def __init__(self, index: int) -> None:
            self.index = index

        def input(self, value: str, clear: bool = False) -> None:
            clear_flags.append(clear)
            # Reproduce the Microsoft six-box focus race: clearing a later
            # box can act on the box that was auto-focused just before it.
            if clear and self.index:
                values[self.index - 1] = ""
            if clear:
                values[self.index] = ""
            values[self.index] = value

    def waiter(page: object, selector: str, description: str, timeout: float) -> Element:
        return Element(int(selector.rsplit("-", 1)[1]))

    app.fill_security_code(
        object(),
        "123456",
        timeout=1,
        waiter=waiter,
        clicker=lambda *args: None,
        sleeper=lambda seconds: None,
    )

    assert values == list("123456")
    assert clear_flags == [False] * 6


def test_fill_security_code_holds_interaction_lock_for_entire_sequence() -> None:
    lock_events: list[str] = []

    class RecordingLock:
        def __enter__(self) -> "RecordingLock":
            lock_events.append("acquire")
            return self

        def __exit__(self, *args: object) -> None:
            lock_events.append("release")

    class Element:
        def input(self, value: str, clear: bool = False) -> None:
            lock_events.append(f"input:{value}:{clear}")

    def waiter(page: object, selector: str, description: str, timeout: float) -> Element:
        return Element()

    def clicker(page: object, selector: str, description: str, timeout: float) -> None:
        lock_events.append(f"click:{selector}")

    app.fill_security_code(
        object(),
        "123456",
        timeout=1,
        waiter=waiter,
        clicker=clicker,
        sleeper=lambda seconds: lock_events.append(f"sleep:{seconds}"),
        interaction_lock=RecordingLock(),
    )

    assert lock_events[0] == "acquire"
    assert lock_events[-1] == "release"
    assert lock_events.count("acquire") == 1
    assert lock_events.count("release") == 1


def test_auxiliary_credentials_file_selects_one_line(tmp_path: Path) -> None:
    path = tmp_path / "辅助邮箱.txt"
    path.write_text(
        "first@example.com----p1----c1----t1\n"
        "second@example.com----p2----c2----t2\n",
        encoding="utf-8",
    )
    selected = app.load_auxiliary_credentials(path, chooser=lambda rows: rows[1])
    assert selected.email == "second@example.com"
    assert selected.token == "t2"


def test_auxiliary_credentials_skips_malformed_rows(tmp_path: Path) -> None:
    path = tmp_path / "辅助邮箱.txt"
    path.write_text(
        "bad-row\nvalid@example.com----p----c----t\n",
        encoding="utf-8",
    )
    selected = app.load_auxiliary_credentials(path, chooser=lambda rows: rows[0])
    assert selected.email == "valid@example.com"


def test_wait_for_url_returns_only_after_loaded() -> None:
    states = iter(
        [
            {"href": "https://login.live.com/", "ready_state": "loading"},
            {"href": "https://account.microsoft.com/?lang=en-US", "ready_state": "complete"},
        ]
    )
    assert app.wait_for_url(
        object(),
        "account.microsoft.com",
        timeout=1,
        state_reader=lambda page: next(states),
        sleeper=lambda seconds: None,
    )["ready_state"] == "complete"


def test_wait_for_alias_entry_detects_late_credential_redirect() -> None:
    states = iter(
        [
            {
                "href": app.ADD_ALIAS_URL,
                "ready_state": "interactive",
            },
            {
                "href": f"{app.CREDENTIAL_ACTION_PREFIX}?mkt=EN-US",
                "ready_state": "complete",
                "associated_id_present": False,
            },
        ]
    )
    state = app.wait_for_alias_entry(
        object(),
        timeout=1,
        state_reader=lambda page: next(states),
        sleeper=lambda seconds: None,
    )
    assert app.is_credential_action_redirect(state["href"])


def test_wait_for_alias_entry_does_not_return_before_redirect_stabilizes() -> None:
    states = iter(
        [
            {
                "href": app.ADD_ALIAS_URL,
                "ready_state": "complete",
                "associated_id_present": True,
            },
            {
                "href": f"{app.CREDENTIAL_ACTION_PREFIX}?mkt=EN-US",
                "ready_state": "complete",
                "associated_id_present": False,
            },
        ]
    )
    state = app.wait_for_alias_entry(
        object(),
        timeout=1,
        state_reader=lambda page: next(states),
        sleeper=lambda seconds: None,
    )
    assert app.is_credential_action_redirect(state["href"])


def test_wait_for_credential_submission_waits_for_route_change() -> None:
    states = iter(
        [
            {"href": f"{app.CREDENTIAL_ACTION_PREFIX}?mkt=EN-US", "code_entry_present": True},
            {"href": app.ADD_ALIAS_URL, "code_entry_present": True},
        ]
    )
    state = app.wait_for_credential_submission(
        object(),
        timeout=1,
        state_reader=lambda page: next(states),
        sleeper=lambda seconds: None,
    )
    assert state["href"] == app.ADD_ALIAS_URL


def test_wait_for_credential_submission_ignores_transient_blank_page() -> None:
    states = iter(
        [
            {"href": f"{app.CREDENTIAL_ACTION_PREFIX}?mkt=EN-US", "code_entry_present": True},
            {"href": "about:blank", "code_entry_present": True},
            {"href": app.ADD_ALIAS_URL, "code_entry_present": True},
        ]
    )
    state = app.wait_for_credential_submission(
        object(),
        timeout=1,
        state_reader=lambda page: next(states),
        sleeper=lambda seconds: None,
    )
    assert state["href"] == app.ADD_ALIAS_URL


def test_open_navigates_and_closes(tmp_path: Path) -> None:
    events: list[tuple[str, object]] = []

    class FakeCore:
        @staticmethod
        def launch_ruyi_browser(proxy: str, **kwargs: object) -> object:
            events.append(("launch", kwargs))
            return object()

        @staticmethod
        def navigate_with_retry(page: object, url: str, description: str, **kwargs: object) -> dict:
            events.append(("navigate", (url, description, kwargs)))
            return {"href": url}

        @staticmethod
        def close_ruyi_browser(page: object, **kwargs: object) -> None:
            events.append(("close", kwargs))

    result = app.open_email_page(
        app.OpenOptions(
            url="https://login.live.com/",
            cache_dir=tmp_path / "cache",
            snapshot_dir=tmp_path / "snapshots",
            timeout=5,
            keep_open=False,
        ),
        core_module=FakeCore,
    )
    assert result == 0
    assert [event[0] for event in events] == ["launch", "navigate", "close"]
    assert events[0][1]["cache_dir"] == tmp_path / "cache"
    assert (tmp_path / "cache").parent.is_dir()
    assert (tmp_path / "snapshots").is_dir()


def test_open_without_credentials_waits_for_enter(tmp_path: Path) -> None:
    events: list[str] = []

    class FakeCore:
        @staticmethod
        def launch_ruyi_browser(proxy: str, **kwargs: object) -> object:
            events.append("launch")
            return object()

        @staticmethod
        def navigate_with_retry(page: object, url: str, description: str, **kwargs: object) -> dict:
            events.append("navigate")
            return {"href": url}

        @staticmethod
        def close_ruyi_browser(page: object, **kwargs: object) -> None:
            events.append("close")

    prompts: list[str] = []
    result = app.open_email_page(
        app.OpenOptions(cache_dir=tmp_path / "cache", snapshot_dir=tmp_path / "snapshots"),
        core_module=FakeCore,
        input_fn=lambda prompt: prompts.append(prompt) or "",
    )
    assert result == 0
    assert prompts == [app.CLOSE_PROMPT]
    assert events == ["launch", "navigate", "close"]


def test_open_with_credentials_runs_workflow_and_waits_for_enter(tmp_path: Path) -> None:
    events: list[tuple[str, object]] = []

    class FakeCore:
        @staticmethod
        def launch_ruyi_browser(proxy: str, **kwargs: object) -> object:
            events.append(("launch", kwargs))
            return object()

        @staticmethod
        def navigate_with_retry(page: object, url: str, description: str, **kwargs: object) -> dict:
            events.append(("navigate", url))
            return {"href": url}

        @staticmethod
        def close_ruyi_browser(page: object, **kwargs: object) -> None:
            events.append(("close", kwargs))

    prompts: list[str] = []
    result = app.open_email_page(
        app.OpenOptions(cache_dir=tmp_path / "cache", snapshot_dir=tmp_path / "snapshots"),
        core_module=FakeCore,
        credentials=app.AccountCredentials(
            email="person@example.com",
            password="secret",
            client_id="client-123",
            token="token-456",
        ),
        input_fn=prompts.append,
        workflow=lambda page, **kwargs: ["person01", "person02"],
    )
    assert result == 0
    assert prompts == ["请检查浏览器中的结果，按 Enter 关闭浏览器..."]
    assert [event[0] for event in events] == ["launch", "navigate", "close"]


def test_open_with_credentials_prints_complete_alias_lines(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    class FakeCore:
        @staticmethod
        def launch_ruyi_browser(proxy: str, **kwargs: object) -> object:
            return object()

        @staticmethod
        def navigate_with_retry(page: object, url: str, description: str, **kwargs: object) -> dict:
            return {"href": url}

        @staticmethod
        def close_ruyi_browser(page: object, **kwargs: object) -> None:
            return None

    app.open_email_page(
        app.OpenOptions(cache_dir=tmp_path / "cache", snapshot_dir=tmp_path / "snapshots", keep_open=False),
        core_module=FakeCore,
        credentials=app.AccountCredentials(
            email="person@example.com",
            password="secret",
            client_id="client-123",
            token="token-456",
        ),
        input_fn=lambda prompt: "",
        workflow=lambda page, **kwargs: ["person01", "person02"],
    )
    assert capsys.readouterr().out == (
        "person01@example.com----secret----client-123----token-456\n"
        "person02@example.com----secret----client-123----token-456\n"
    )


def test_open_error_waits_for_enter_before_close(tmp_path: Path) -> None:
    events: list[str] = []
    prompts: list[str] = []

    class FakeCore:
        @staticmethod
        def launch_ruyi_browser(proxy: str, **kwargs: object) -> object:
            events.append("launch")
            return object()

        @staticmethod
        def navigate_with_retry(page: object, url: str, description: str, **kwargs: object) -> dict:
            events.append("navigate")
            return {"href": url}

        @staticmethod
        def close_ruyi_browser(page: object, **kwargs: object) -> None:
            events.append("close")

    def input_fn(prompt: str) -> str:
        prompts.append(prompt)
        events.append("enter")
        return ""

    result = app.open_email_page(
        app.OpenOptions(
            cache_dir=tmp_path / "cache",
            snapshot_dir=tmp_path / "snapshots",
            keep_open=False,
        ),
        core_module=FakeCore,
        credentials=app.AccountCredentials(
            email="person@example.com",
            password="secret",
            client_id="client-123",
            token="token-456",
        ),
        input_fn=input_fn,
        workflow=lambda page, **kwargs: (_ for _ in ()).throw(RuntimeError("alias failed")),
    )
    assert result == 1
    assert events == ["launch", "navigate", "enter", "close"]
    assert prompts == ["请检查浏览器中的结果，按 Enter 关闭浏览器..."]


def test_open_closes_when_enter_prompt_raises(tmp_path: Path) -> None:
    events: list[str] = []

    class FakeCore:
        @staticmethod
        def launch_ruyi_browser(proxy: str, **kwargs: object) -> object:
            events.append("launch")
            return object()

        @staticmethod
        def navigate_with_retry(page: object, url: str, description: str, **kwargs: object) -> dict:
            events.append("navigate")
            return {"href": url}

        @staticmethod
        def close_ruyi_browser(page: object, **kwargs: object) -> None:
            events.append("close")

    def broken_input(prompt: str) -> str:
        events.append("prompt")
        raise RuntimeError("terminal input failed")

    result = app.open_email_page(
        app.OpenOptions(cache_dir=tmp_path / "cache", snapshot_dir=tmp_path / "snapshots"),
        core_module=FakeCore,
        input_fn=broken_input,
    )
    assert result == 0
    assert events == ["launch", "navigate", "prompt", "close"]


def test_clear_cache_removes_profile(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "old-entry").write_text("stale", encoding="utf-8")
    app.clear_profile(cache)
    assert not cache.exists()


def test_clear_cache_happens_before_launch(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "old-entry").write_text("stale", encoding="utf-8")
    seen: list[bool] = []

    class FakeCore:
        @staticmethod
        def launch_ruyi_browser(proxy: str, **kwargs: object) -> object:
            seen.append((cache / "old-entry").exists())
            return object()

        @staticmethod
        def navigate_with_retry(page: object, url: str, description: str, **kwargs: object) -> dict:
            return {"href": url}

        @staticmethod
        def close_ruyi_browser(page: object, **kwargs: object) -> None:
            return None

    result = app.open_email_page(
        app.OpenOptions(
            cache_dir=cache,
            snapshot_dir=tmp_path / "snapshots",
            keep_open=False,
            clear_cache=True,
        ),
        core_module=FakeCore,
    )
    assert result == 0
    assert seen == [False]


def test_login_and_add_aliases_uses_two_tabs() -> None:
    events: list[tuple[str, object]] = []
    lock_events: list[str] = []

    class RecordingLock:
        def __enter__(self) -> "RecordingLock":
            lock_events.append("acquire")
            return self

        def __exit__(self, *args: object) -> None:
            lock_events.append("release")

    class FakeElement:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        def input(self, value: str, clear: bool = False) -> None:
            events.append(("input", (self.selector, value, clear)))

        def click(self, **kwargs: object) -> None:
            events.append(("click", (self.selector, kwargs)))

    class FakeTab:
        def __init__(self, href: str = "https://account.live.com/AddAssocId") -> None:
            self.href = href

        def ele(self, selector: str, timeout: float = 0.25) -> FakeElement | None:
            if selector in {
                "#usernameEntry",
                "#passwordEntry",
                "#AssociatedIdLive",
                "#SubmitYes",
                app.PRIMARY_BUTTON_SELECTOR,
            }:
                return FakeElement(selector)
            if selector == "#idAliasEmail0" and self.href.startswith("https://account.live.com/names"):
                return FakeElement(selector)
            if selector == "#idAliasEmail1" and self.href.startswith("https://account.live.com/names"):
                return FakeElement(selector)
            return None

        def get(self, url: str, **kwargs: object) -> None:
            self.href = url
            events.append(("get", url))

        def run_js(self, script: str, **kwargs: object) -> object:
            if "location.href" in script:
                return self.href
            return "complete"

    class FakePage(FakeTab):
        def __init__(self) -> None:
            super().__init__("https://login.live.com/")
            self.tabs: list[FakeTab] = []

        def new_tab(self, url: str, background: bool = False) -> FakeTab:
            tab = FakeTab(url)
            self.tabs.append(tab)
            events.append(("new_tab", (url, background)))
            return tab

    page = FakePage()

    def clicker(target: FakeTab, selector: str, description: str, timeout: float) -> None:
        target.ele(selector).click()
        if selector == app.PRIMARY_BUTTON_SELECTOR and target.href.startswith("https://login.live.com"):
            target.href = "https://account.microsoft.com/?lang=en-US"
        elif selector == "#SubmitYes":
            target.href = "https://account.live.com/names"

    result = app.login_and_add_aliases(
        page,
        email="person@example.com",
        password="secret",
        timeout=1,
        waiter=lambda target, selector, description, timeout: target.ele(selector),
        clicker=clicker,
        navigator=lambda target, url, description, **kwargs: target.get(url),
        state_reader=lambda target: {"href": target.href, "ready_state": "complete"},
        sleeper=lambda seconds: None,
        interaction_lock=RecordingLock(),
    )
    assert result == ["person01", "person02"]
    assert ("input", ("#usernameEntry", "person@example.com", True)) in events
    assert ("input", ("#passwordEntry", "secret", True)) in events
    assert ("input", ("#AssociatedIdLive", "person01", True)) in events
    assert ("input", ("#AssociatedIdLive", "person02", True)) in events
    assert events.count(("new_tab", ("about:blank", False))) == 2
    assert lock_events
    assert lock_events.count("acquire") == lock_events.count("release")


def test_credential_action_redirect_adds_auxiliary_email_before_aliases() -> None:
    events: list[tuple[str, object]] = []

    class FakeElement:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        def input(self, value: str, clear: bool = False) -> None:
            events.append(("input", (self.selector, value, clear)))

        def click(self, **kwargs: object) -> None:
            events.append(("click", (self.selector, kwargs)))

    class FakeTab:
        def __init__(self, href: str = app.ADD_ALIAS_URL) -> None:
            self.href = href

        def ele(self, selector: str, timeout: float = 0.25) -> FakeElement | None:
            if selector in {
                app.USERNAME_SELECTOR,
                app.PASSWORD_SELECTOR,
                app.ASSOCIATED_ID_SELECTOR,
                app.SUBMIT_YES_SELECTOR,
                app.PRIMARY_BUTTON_SELECTOR,
                "#floatingLabelInput10",
                "#iMarkLost",
                "#CommitMiniBtn",
            }:
                return FakeElement(selector)
            if selector.startswith("#codeEntry-"):
                return FakeElement(selector)
            if selector in {"#idAliasEmail0", "#idAliasEmail1"} and self.href.startswith(app.ACCOUNT_NAMES_PREFIX):
                return FakeElement(selector)
            return None

        def get(self, url: str, **kwargs: object) -> None:
            self.href = url
            events.append(("get", url))

        def run_js(self, script: str, **kwargs: object) -> object:
            if "location.href" in script:
                return self.href
            return {"href": self.href, "ready_state": "complete"}

    class FakePage(FakeTab):
        def __init__(self) -> None:
            super().__init__("https://account.microsoft.com/?lang=en-US")

        def new_tab(self, url: str, background: bool = False) -> FakeTab:
            tab = FakeTab(url)
            events.append(("new_tab", (url, background)))
            return tab

    page = FakePage()
    add_assoc_calls = 0
    sent_at = dt.datetime(2026, 9, 13, 11, 0, tzinfo=dt.timezone.utc)
    code_not_before: list[dt.datetime] = []

    def navigator(target: FakeTab, url: str, description: str, **kwargs: object) -> None:
        nonlocal add_assoc_calls
        add_assoc_calls += 1
        target.get(url)
        if add_assoc_calls == 1:
            target.href = "https://account.live.com/interrupt/credentialaction?mkt=EN-US"

    def clicker(target: FakeTab, selector: str, description: str, timeout: float) -> None:
        target.ele(selector).click()
        if selector == app.PRIMARY_BUTTON_SELECTOR and target.href.startswith("https://account.microsoft.com"):
            target.href = "https://account.microsoft.com/?lang=en-US"
        elif selector == app.SUBMIT_YES_SELECTOR:
            target.href = app.ACCOUNT_NAMES_PREFIX
        elif selector == "#CommitMiniBtn":
            target.href = app.ADD_ALIAS_URL

    def security_code_fetcher(credentials: app.AccountCredentials, **kwargs: object) -> str:
        code_not_before.append(kwargs["not_before"])
        return "524719"

    aliases = app.login_and_add_aliases(
        page,
        email="person@example.com",
        password="secret",
        timeout=1,
        waiter=lambda target, selector, description, timeout: target.ele(selector),
        clicker=clicker,
        navigator=navigator,
        state_reader=lambda target: {
            "href": target.href,
            "ready_state": "complete",
            "associated_id_present": target.href.startswith(app.ADD_ALIAS_URL),
        },
        sleeper=lambda seconds: None,
        utcnow=lambda: sent_at,
        auxiliary_loader=lambda path: app.AccountCredentials(
            email="helper@example.com",
            password="helper-pass",
            client_id="helper-client",
            token="helper-token",
        ),
        security_code_fetcher=security_code_fetcher,
    )
    assert aliases == ["person01", "person02"]
    assert ("input", ("#floatingLabelInput10", "helper@example.com", True)) in events
    assert ("input", ("#codeEntry-0", "5", False)) in events
    assert ("input", ("#codeEntry-5", "9", False)) in events
    assert ("click", ("#iMarkLost", {})) in events
    assert ("click", ("#CommitMiniBtn", {})) in events
    assert add_assoc_calls == 3
    assert code_not_before == [sent_at]


def test_credential_action_timeout_records_stage_and_screenshot(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class FakeElement:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        def input(self, value: str, clear: bool = False) -> None:
            return None

        def click(self, **kwargs: object) -> None:
            return None

    class FakeTab:
        def __init__(self, href: str = app.ADD_ALIAS_URL) -> None:
            self.href = href

        def ele(self, selector: str, timeout: float = 0.25) -> FakeElement | None:
            if selector == "#floatingLabelInput10":
                return None
            if selector.startswith("#codeEntry-"):
                return FakeElement(selector)
            return FakeElement(selector)

        def get(self, url: str, **kwargs: object) -> None:
            self.href = url

        def run_js(self, script: str, **kwargs: object) -> object:
            return {"href": self.href, "ready_state": "complete"}

        def screenshot(self, path: str, **kwargs: object) -> None:
            Path(path).write_bytes(b"png")

    class FakePage(FakeTab):
        def new_tab(self, url: str, background: bool = False) -> FakeTab:
            return FakeTab(url)

    page = FakePage("https://account.microsoft.com/?lang=en-US")

    def navigator(target: FakeTab, url: str, description: str, **kwargs: object) -> None:
        target.get(url)
        target.href = "https://account.live.com/interrupt/credentialaction?mkt=EN-US"

    def waiter(target: FakeTab, selector: str, description: str, timeout: float) -> FakeElement:
        if selector == "#floatingLabelInput10":
            raise TimeoutError("placeholder")
        return FakeElement(selector)

    with caplog.at_level("INFO", logger=app.LOG.name):
        with pytest.raises(TimeoutError):
            app.login_and_add_aliases(
                page,
                email="person@example.com",
                password="secret",
                timeout=1,
                waiter=waiter,
                clicker=lambda *args, **kwargs: None,
                navigator=navigator,
                state_reader=lambda target: {
                    "href": target.href,
                    "ready_state": "complete",
                },
                sleeper=lambda seconds: None,
                auxiliary_loader=lambda path: app.AccountCredentials(
                    email="helper@example.com",
                    password="helper-pass",
                    client_id="helper-client",
                    token="helper-token",
                ),
                diagnostic_dir=tmp_path / "diagnostics",
            )

    snapshots = list((tmp_path / "diagnostics").glob("credentialaction-alias-1-1-auxiliary-input*.png"))
    assert snapshots and snapshots[0].read_bytes() == b"png"
    assert "credentialaction-alias-1-1-auxiliary-input" in caplog.text


def test_fetch_security_code_falls_back_to_mode4_o2() -> None:
    class BrokenFetcher:
        class requests:
            class Session:
                def __enter__(self) -> "BrokenFetcher.requests.Session":
                    return self

                def __exit__(self, *args: object) -> None:
                    return None

        @staticmethod
        def get_access_token(session: object, client_id: str, token: str) -> tuple[str, str]:
            raise RuntimeError("graph unavailable")

    credentials = app.AccountCredentials(
        email="helper@example.com",
        password="helper-pass",
        client_id="helper-client",
        token="helper-token",
    )
    calls: list[app.AccountCredentials] = []

    def o2_fetcher(value: app.AccountCredentials, **kwargs: object) -> str:
        calls.append(value)
        return "524719"

    assert app.fetch_security_code(
        credentials,
        timeout=1,
        fetcher_module=BrokenFetcher,
        o2_fetcher=o2_fetcher,
    ) == "524719"
    assert calls == [credentials]


def test_find_security_code_filters_old_sender_and_target() -> None:
    cutoff = dt.datetime(2026, 9, 13, 11, 0, tzinfo=dt.timezone.utc)
    messages = [
        {
            "from": {"emailAddress": {"name": "Microsoft account team", "address": "no-reply@microsoft.com"}},
            "receivedDateTime": "2026-09-13T10:59:00Z",
            "body": {"content": "Security code: 111111"},
        },
        {
            "from": {"emailAddress": {"name": "Someone else", "address": "x@example.com"}},
            "receivedDateTime": "2026-09-13T11:01:00Z",
            "body": {"content": "Security code: 222222"},
        },
        {
            "from": {"emailAddress": {"name": "Microsoft account team", "address": "no-reply@microsoft.com"}},
            "receivedDateTime": "2026-09-13T11:02:00Z",
            "body": {"content": "Security code: 333333 for Ta**4@outlook.com"},
        },
    ]
    assert app._find_security_code_in_messages(
        messages,
        not_before=cutoff,
        target_email="Tallperson4@outlook.com",
    ) == "333333"


def test_microsoft_sender_address_is_accepted_without_display_name() -> None:
    assert app._message_sender_is_microsoft_account_team(
        {
            "from": {
                "emailAddress": {
                    "name": "",
                    "address": "account-security-noreply@accountprotection.microsoft.com",
                }
            }
        }
    )


def test_graph_mail_fetch_follows_odata_next_link() -> None:
    class Response:
        status_code = 200

        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def json(self) -> dict:
            return self.payload

        def raise_for_status(self) -> None:
            return None

    class Session:
        def __init__(self) -> None:
            self.responses = [
                Response({"value": [], "@odata.nextLink": "https://graph.example/next"}),
                Response(
                    {
                        "value": [
                            {
                                "from": {"emailAddress": {"name": "Microsoft account team"}},
                                "body": {"content": "Security code: 524719"},
                            }
                        ]
                    }
                ),
            ]
            self.urls: list[str] = []

        def __enter__(self) -> "Session":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, url: str, **kwargs: object) -> Response:
            self.urls.append(url)
            return self.responses.pop(0)

    session = Session()
    Fetcher = type(
        "Fetcher",
        (),
        {
            "requests": type("Requests", (), {"Session": lambda: session}),
            "MESSAGES_URL": "https://graph.example/messages",
            "get_access_token": staticmethod(lambda *args: ("access", "refresh")),
        },
    )
    assert app.fetch_security_code(
        app.AccountCredentials("helper@example.com", "p", "c", "t"),
        timeout=1,
        fetcher_module=Fetcher,
        o2_fetcher=lambda *args, **kwargs: "o2-code",
    ) == "524719"
    assert session.urls == ["https://graph.example/messages", "https://graph.example/next"]


def test_graph_mail_fetch_refreshes_after_401() -> None:
    class Response:
        def __init__(self, status_code: int, payload: dict) -> None:
            self.status_code = status_code
            self.payload = payload

        def json(self) -> dict:
            return self.payload

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

    class Session:
        def __init__(self) -> None:
            self.responses = [
                Response(401, {}),
                Response(
                    200,
                    {
                        "value": [
                            {
                                "from": {"emailAddress": {"name": "Microsoft account team"}},
                                "body": {"content": "Security code: 524719"},
                            }
                        ]
                    },
                ),
            ]
            self.tokens: list[str] = []

        def __enter__(self) -> "Session":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, url: str, **kwargs: object) -> Response:
            self.tokens.append(str(kwargs["headers"]["Authorization"]))
            return self.responses.pop(0)

    session = Session()
    refresh_calls: list[str] = []

    def get_access_token(session_value: object, client_id: str, refresh_token: str) -> tuple[str, str]:
        refresh_calls.append(refresh_token)
        return (f"access-{len(refresh_calls)}", f"refresh-{len(refresh_calls)}")

    Fetcher = type(
        "Fetcher",
        (),
        {
            "requests": type("Requests", (), {"Session": lambda: session}),
            "MESSAGES_URL": "https://graph.example/messages",
            "get_access_token": staticmethod(get_access_token),
        },
    )
    assert app.fetch_security_code(
        app.AccountCredentials("helper@example.com", "p", "c", "t"),
        timeout=1,
        fetcher_module=Fetcher,
        o2_fetcher=lambda *args, **kwargs: "o2-code",
        sleeper=lambda seconds: None,
    ) == "524719"
    assert refresh_calls == ["t", "refresh-1"]
    assert session.tokens[-1] == "Bearer access-2"


def test_fetch_security_code_o2_normalizes_rows() -> None:
    class Response:
        def __init__(self, payload: dict) -> None:
            self.status_code = 200
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class Session:
        def __init__(self) -> None:
            self.posts: list[tuple[str, dict]] = []

        def __enter__(self) -> "Session":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def post(self, url: str, **kwargs: object) -> Response:
            self.posts.append((url, kwargs["json"]))
            if url.endswith("detect-permission"):
                return Response({"success": True, "token_type": "o2"})
            return Response(
                {
                    "success": True,
                    "data": [
                        {
                            "from_name": "Microsoft account team",
                            "from_address": "no-reply@microsoft.com",
                            "body_preview": "Security code: 524719 for Ta**4@outlook.com",
                            "received_time": "2026-09-13T11:01:00Z",
                        }
                    ],
                }
            )

    Requests = type("Requests", (), {"Session": Session})

    assert app.fetch_security_code_o2(
        app.AccountCredentials("helper@example.com", "p", "c", "t"),
        timeout=1,
        target_email="Tallperson4@outlook.com",
        requests_module=Requests,
    ) == "524719"


def test_alias_prefix_requires_email() -> None:
    with pytest.raises(ValueError, match="邮箱"):
        app.email_prefix("not-an-email")
